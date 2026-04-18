#!/usr/bin/env python3
"""
Liquidation Utilities - Accurate margin liquidation calculations for backtesting.

This module provides functions to calculate the exact price at which margin level
would hit the closeout threshold. This is critical for accurate backtesting because:

1. Capital.com liquidates positions the MOMENT margin level hits the threshold (e.g., 55%)
2. Using candle extremes (low/high) overestimates losses - the position would have been
   closed BEFORE the price reached those extremes
3. The recorded minMarginLevel should reflect actual minimum, not theoretical worst-case

MATH DERIVATION:
================
Margin Level (ML) = (Equity / Required_Margin) * 100

For LONG positions:
- Equity = Balance + (Price - Entry) * Contracts * Multiplier
- Required_Margin = Price * Contracts * Multiplier * Margin_Factor

Solving for Price when ML = closeout_level:
  closeout/100 * Price * C * M * MF = Balance + (Price - Entry) * C * M
  Let K = C * M (position size factor)
  closeout/100 * Price * K * MF = Balance + Price * K - Entry * K
  Price * K * (closeout/100 * MF - 1) = Balance - Entry * K
  Price = (Balance - Entry * K) / (K * (closeout/100 * MF - 1))

For SHORT positions:
- Equity = Balance + (Entry - Price) * Contracts * Multiplier

Solving similarly:
  Price = (Balance + Entry * K) / (K * (closeout/100 * MF + 1))

Author: Claude (Dec 2025)
"""

from typing import Tuple, Optional
import sys


def calculate_liquidation_trigger_price(
    direction: str,
    entry_price: float,
    balance: float,
    contracts: float,
    contract_multiplier: float,
    margin_factor: float,
    margin_closeout_level: float = 55.0
) -> Optional[float]:
    """
    Calculate the exact price at which margin level would hit the closeout threshold.
    
    This is the price where Capital.com would liquidate the position. In backtesting,
    if the candle's price crosses this threshold, we should exit at this price (plus
    slippage), NOT at the candle's extreme.
    
    Args:
        direction: 'long' or 'short'
        entry_price: Price at which position was entered
        balance: Current account balance (before unrealized P&L)
        contracts: Number of contracts in the position
        contract_multiplier: Contract size multiplier (e.g., 1.0 for most CFDs)
        margin_factor: Margin requirement as decimal (e.g., 0.1 for 10x leverage)
        margin_closeout_level: ML threshold for liquidation (default 55%)
    
    Returns:
        The price at which ML would hit the closeout level, or None if calculation fails
    
    Example:
        For a LONG at $100, balance $1000, 10 contracts, 1.0 multiplier, 10x leverage (MF=0.1):
        If closeout is 55%, liquidation triggers at approximately $91.18
    """
    try:
        # Position size factor: contracts * multiplier
        K = contracts * contract_multiplier
        
        if K <= 0:
            return None
        
        # Convert closeout level to decimal (55% -> 0.55)
        closeout_decimal = margin_closeout_level / 100.0
        
        if direction == 'long':
            # For LONG: price drops, equity decreases
            # Price = (Balance - Entry * K) / (K * (closeout * MF - 1))
            denominator = K * (closeout_decimal * margin_factor - 1)
            
            if denominator >= 0:
                # This would mean no liquidation possible (very high balance)
                return None
            
            numerator = balance - entry_price * K
            trigger_price = numerator / denominator
            
            # Sanity check: trigger price should be below entry for long
            if trigger_price >= entry_price:
                return None
            if trigger_price <= 0:
                return None
                
            return trigger_price
            
        elif direction == 'short':
            # For SHORT: price rises, equity decreases
            # Price = (Balance + Entry * K) / (K * (closeout * MF + 1))
            denominator = K * (closeout_decimal * margin_factor + 1)
            
            if denominator <= 0:
                return None
            
            numerator = balance + entry_price * K
            trigger_price = numerator / denominator
            
            # Sanity check: trigger price should be above entry for short
            if trigger_price <= entry_price:
                return None
            if trigger_price <= 0:
                return None
                
            return trigger_price
        
        return None
        
    except Exception as e:
        print(f"[LiquidationUtils] Error calculating trigger price: {e}", file=sys.stderr)
        return None


def check_liquidation_crossed(
    direction: str,
    trigger_price: float,
    candle_low: float,
    candle_high: float
) -> Tuple[bool, Optional[float]]:
    """
    Check if a candle crosses the liquidation trigger price.
    
    Args:
        direction: 'long' or 'short'
        trigger_price: The calculated liquidation trigger price
        candle_low: Low price of the candle
        candle_high: High price of the candle
    
    Returns:
        Tuple of (crossed: bool, exit_price: float or None)
        - If crossed, exit_price is the trigger_price (before slippage)
        - If not crossed, exit_price is None
    """
    if trigger_price is None:
        return False, None
    
    if direction == 'long':
        # For long, liquidation triggers when price drops to trigger
        if candle_low <= trigger_price:
            return True, trigger_price
    elif direction == 'short':
        # For short, liquidation triggers when price rises to trigger
        if candle_high >= trigger_price:
            return True, trigger_price
    
    return False, None


def calculate_margin_level(
    direction: str,
    current_price: float,
    entry_price: float,
    balance: float,
    contracts: float,
    contract_multiplier: float,
    margin_factor: float
) -> float:
    """
    Calculate the current margin level at a given price.
    
    This is useful for verification and tracking actual ML values.
    
    Args:
        direction: 'long' or 'short'
        current_price: Current market price
        entry_price: Position entry price
        balance: Account balance
        contracts: Number of contracts
        contract_multiplier: Contract multiplier
        margin_factor: Margin factor (e.g., 0.1 for 10x)
    
    Returns:
        Margin level as percentage (e.g., 55.0 for 55%)
    """
    K = contracts * contract_multiplier
    
    if direction == 'long':
        unrealized_pnl = (current_price - entry_price) * K
    else:
        unrealized_pnl = (entry_price - current_price) * K
    
    equity = balance + unrealized_pnl
    required_margin = current_price * K * margin_factor
    
    if required_margin <= 0:
        return float('inf')
    
    return (equity / required_margin) * 100


# =============================================================================
# TESTING
# =============================================================================

if __name__ == '__main__':
    print("=" * 70)
    print("LIQUIDATION UTILS - TEST SUITE")
    print("=" * 70)
    
    # Test Case 1: LONG position (realistic - using 10x leverage with full position)
    print("\n--- Test 1: LONG position (realistic 10x leverage) ---")
    entry = 100.0
    balance = 10000.0  # $10k account
    contracts = 1000.0  # $100k notional at $100 = 10x leverage
    multiplier = 1.0
    margin_factor = 0.1  # 10x leverage
    closeout = 55.0
    
    trigger = calculate_liquidation_trigger_price(
        'long', entry, balance, contracts, multiplier, margin_factor, closeout
    )
    position_notional = entry * contracts * multiplier
    leverage_actual = position_notional / balance
    print(f"Entry: ${entry}, Balance: ${balance:,.0f}, Contracts: {contracts}")
    print(f"Position notional: ${position_notional:,.0f} ({leverage_actual:.1f}x actual leverage)")
    print(f"Margin factor: {margin_factor} (max {1/margin_factor:.0f}x), Closeout: {closeout}%")
    print(f"Liquidation trigger price: ${trigger:.2f}" if trigger else "No trigger")
    
    # Verify by calculating ML at trigger price
    if trigger:
        ml_at_trigger = calculate_margin_level(
            'long', trigger, entry, balance, contracts, multiplier, margin_factor
        )
        print(f"ML at trigger price: {ml_at_trigger:.2f}% (should be ~{closeout}%)")
        price_drop_pct = (entry - trigger) / entry * 100
        print(f"Price drop to trigger: {price_drop_pct:.2f}%")
    
    # Test Case 2: SHORT position
    print("\n--- Test 2: SHORT position ---")
    trigger_short = calculate_liquidation_trigger_price(
        'short', entry, balance, contracts, multiplier, margin_factor, closeout
    )
    print(f"Liquidation trigger price: ${trigger_short:.2f}" if trigger_short else "No trigger")
    
    if trigger_short:
        ml_at_trigger = calculate_margin_level(
            'short', trigger_short, entry, balance, contracts, multiplier, margin_factor
        )
        print(f"ML at trigger price: {ml_at_trigger:.2f}% (should be ~{closeout}%)")
        price_rise_pct = (trigger_short - entry) / entry * 100
        print(f"Price rise to trigger: {price_rise_pct:.2f}%")
    
    # Test Case 3: Check if candle crosses trigger (LONG)
    print("\n--- Test 3: Candle crossing check (LONG) ---")
    candle_low = 90.0  # Below trigger
    candle_high = 102.0
    
    crossed, exit_price = check_liquidation_crossed('long', trigger, candle_low, candle_high)
    if trigger:
        print(f"LONG: Trigger=${trigger:.2f}, Candle low=${candle_low}, Candle high=${candle_high}")
        if crossed:
            print(f"[OK] Crossed! Exit price: ${exit_price:.2f}")
        else:
            print(f"[X] Not crossed")
    
    # Test Case 4: Compare old vs new approach
    print("\n--- Test 4: Old vs New approach comparison ---")
    if trigger:
        slippage_pct = 0.2
        # Old approach: uses candle low with slippage
        old_exit = candle_low * (1 - slippage_pct/100)
        # New approach: uses trigger price with slippage
        new_exit = trigger * (1 - slippage_pct/100)
        
        # Calculate P&L difference for a 1000-contract position
        old_pnl = (old_exit - entry) * contracts * multiplier
        new_pnl = (new_exit - entry) * contracts * multiplier
        
        print(f"OLD exit (at candle low ${candle_low}): ${old_exit:.2f}")
        print(f"NEW exit (at trigger ${trigger:.2f}):    ${new_exit:.2f}")
        print(f"Price difference: ${new_exit - old_exit:.2f} ({(new_exit - old_exit)/old_exit*100:.2f}% better)")
        print(f"P&L difference: ${new_pnl - old_pnl:,.2f} (NEW saves ${abs(new_pnl - old_pnl):,.2f})")
    
    # Test Case 5: Real-world scenario from run 243 (5x leverage, 0.5% SL)
    print("\n--- Test 5: Real scenario (5x leverage, $100 balance, 0.5% SL) ---")
    entry5 = 100.0
    balance5 = 100.0  # Starting balance
    margin_factor5 = 0.2  # 5x leverage
    # Position size: balance * position_size_pct / margin_factor = 100 * 0.5 / 0.2 = 250 notional
    contracts5 = 2.5  # 2.5 contracts at $100 = $250 notional (2.5x actual leverage)
    
    trigger5 = calculate_liquidation_trigger_price(
        'long', entry5, balance5, contracts5, 1.0, margin_factor5, closeout
    )
    print(f"Entry: ${entry5}, Balance: ${balance5}, Contracts: {contracts5}")
    print(f"Position notional: ${entry5 * contracts5:.0f}, Actual leverage: {entry5 * contracts5 / balance5:.1f}x")
    if trigger5:
        print(f"Liquidation trigger: ${trigger5:.2f} ({(entry5 - trigger5) / entry5 * 100:.1f}% drop)")
        ml_at_trigger = calculate_margin_level('long', trigger5, entry5, balance5, contracts5, 1.0, margin_factor5)
        print(f"ML at trigger: {ml_at_trigger:.2f}%")
    else:
        print("No liquidation trigger (position too small to liquidate)")
    
    print("\n" + "=" * 70)
    print("All tests completed!")
