import pandas as pd
import numpy as np
from datetime import datetime, timedelta, time
import os
import warnings
import talib
warnings.filterwarnings('ignore')

# Suppress banner output when used as a module
# print("FOCUSED STRATEGY OPTIMIZER")
# print("=" * 80)
# print("Focused optimization on key parameters:")
# print("• Best leverage levels (2x, 3x, 4x)")
# print("• Best stop losses (2%, 3%, 4%)")
# print("• Best AI confidence (30%, 40%, 50%)")
# print("• 99% investment (maximum growth)")
# print("• Hybrid Intelligent vs Daily Trading vs Buy & Hold")
# print("-" * 80)

# Real Capital.com fees
SPREAD_COST = 0.00019  # 0.019% per trade
OVERNIGHT_FUNDING_LONG = -0.00023  # -0.023% per day for longs

def calculate_technical_indicators(df):
    """Calculate technical indicators for AI analysis"""
    df = df.copy()
    
    # Price-based indicators
    df['sma_20'] = talib.SMA(df['closePrice'], timeperiod=20)
    df['ema_12'] = talib.EMA(df['closePrice'], timeperiod=12)
    
    # Momentum indicators
    df['rsi'] = talib.RSI(df['closePrice'], timeperiod=14)
    df['macd'], df['macd_signal'], df['macd_hist'] = talib.MACD(df['closePrice'])
    
    # Volatility indicators
    df['atr'] = talib.ATR(df['highPrice'], df['lowPrice'], df['closePrice'], timeperiod=14)
    
    # Volume indicators
    volume_col = 'lastTradedVolume' if 'lastTradedVolume' in df.columns else 'volume'
    if volume_col in df.columns:
        df['volume_sma'] = talib.SMA(df[volume_col], timeperiod=20)
        df['volume_ratio'] = df[volume_col] / df['volume_sma']
    else:
        df['volume_sma'] = 1.0
        df['volume_ratio'] = 1.0
    
    # Price patterns
    df['price_change'] = df['closePrice'].pct_change()
    df['price_momentum'] = df['closePrice'].pct_change(periods=5)
    
    return df

def analyze_market_conditions(day_data, historical_data, current_date):
    """AI-powered market condition analysis"""
    
    if day_data.empty or len(historical_data) < 50:
        return {
            'trade_signal': 'hold',
            'confidence': 0.0,
            'direction': 'long'
        }
    
    # Get latest indicators
    latest = historical_data.iloc[-1]
    
    # Initialize scoring system
    bullish_signals = 0
    bearish_signals = 0
    
    # 1. Trend Analysis
    if latest['closePrice'] > latest['sma_20']:
        bullish_signals += 3
    else:
        bearish_signals += 3
    
    # 2. Momentum Analysis
    if 30 < latest['rsi'] < 70:
        bullish_signals += 2
    elif latest['rsi'] > 70:
        bearish_signals += 2
    elif latest['rsi'] < 30:
        bullish_signals += 2  # Oversold can be bullish
    
    if latest['macd'] > latest['macd_signal']:
        bullish_signals += 2
    else:
        bearish_signals += 2
    
    # 3. Volume Analysis
    if latest['volume_ratio'] > 1.2:
        bullish_signals += 1
    elif latest['volume_ratio'] < 0.8:
        bearish_signals += 1
    
    # 4. Day of Week Bias
    day_of_week = current_date.strftime('%A')
    if day_of_week == 'Tuesday':
        bullish_signals += 3
    elif day_of_week == 'Friday':
        bullish_signals += 2
    elif day_of_week == 'Wednesday':
        bullish_signals += 1
    elif day_of_week == 'Thursday':
        bearish_signals += 3
    
    # 5. Recent Performance
    recent_returns = historical_data['price_change'].tail(5).mean()
    if recent_returns > 0.01:
        bullish_signals += 2
    elif recent_returns < -0.01:
        bearish_signals += 2
    
    # Calculate final decision
    total_signals = bullish_signals + bearish_signals
    if total_signals == 0:
        return {
            'trade_signal': 'hold',
            'confidence': 0.0,
            'direction': 'long'
        }
    
    bullish_ratio = bullish_signals / total_signals
    confidence = abs(bullish_ratio - 0.5) * 2  # 0 to 1 scale
    
    if bullish_ratio > 0.65:
        trade_signal = 'buy'
        direction = 'long'
    elif bullish_ratio < 0.35:
        trade_signal = 'sell'
        direction = 'short'
    else:
        trade_signal = 'hold'
        direction = 'long'
    
    return {
        'trade_signal': trade_signal,
        'confidence': confidence,
        'direction': direction
    }

def calculate_trading_costs(position_size, direction, is_overnight=True):
    """Calculate real Capital.com trading costs
    
    Per Capital.com docs: spread is paid once per round-trip (half at entry, half at exit = 1x total)
    """
    spread_cost = position_size * SPREAD_COST  # Single spread per round-trip (NOT * 2)
    
    overnight_cost = 0
    if is_overnight:
        overnight_cost = position_size * OVERNIGHT_FUNDING_LONG
    
    return spread_cost + overnight_cost

def check_stop_loss_hit(day_data, entry_price, entry_time, direction, stop_loss_pct):
    """Check if stop loss is hit during the day"""
    if day_data.empty:
        return False, None, None
    
    trading_data = day_data[day_data['time'] > entry_time].copy()
    
    if trading_data.empty:
        return False, None, None
    
    for _, row in trading_data.iterrows():
        if direction == 'long':
            stop_price = entry_price * (1 - stop_loss_pct / 100)
            if row['lowPrice'] <= stop_price:
                return True, stop_price, row['time']
        else:
            stop_price = entry_price * (1 + stop_loss_pct / 100)
            if row['highPrice'] >= stop_price:
                return True, stop_price, row['time']
    
    return False, None, None

def get_price_at_time(day_data, target_time, price_type='closePrice', max_time_diff_seconds=300):
    """Get price at specific time, or closest available within tolerance
    
    Args:
        day_data: DataFrame with candle data for the day
        target_time: Target time to find price at
        price_type: Which price to return (closePrice, openPrice, etc)
        max_time_diff_seconds: Maximum allowed time difference (default 5 minutes)
    
    Returns:
        Price at or near target time, or None if no candle within tolerance
    """
    if day_data.empty:
        return None
    
    day_data = day_data.copy()
    day_data['time_diff'] = day_data['time'].apply(
        lambda t: abs((datetime.combine(datetime.today(), t) - 
                      datetime.combine(datetime.today(), target_time)).total_seconds())
    )
    
    closest_row = day_data.loc[day_data['time_diff'].idxmin()]
    
    # Reject if closest candle is too far from target time
    if closest_row['time_diff'] > max_time_diff_seconds:
        return None  # No valid price found within tolerance
    
    return closest_row[price_type]

def is_trading_day(date):
    """Check if date is a trading day (Monday-Friday)"""
    return date.weekday() < 5

def run_hybrid_intelligent_strategy(df, leverage, stop_loss_pct, confidence_threshold):
    """Hybrid Intelligent Trading Strategy"""
    
    # Prepare data with technical indicators
    df_with_indicators = calculate_technical_indicators(df)
    
    # Get all trading days
    all_dates = sorted(df['date'].unique())
    trading_days = [date for date in all_dates if is_trading_day(date)]
    
    if len(trading_days) < 20:
        return None
    
    # Trading parameters
    balance = 100.0
    total_contributions = 0
    total_fees = 0
    winning_trades = 0
    losing_trades = 0
    max_drawdown = 0
    peak_balance = 100.0
    
    # Monthly contribution dates
    contribution_dates = []
    monthly_groups = {}
    for date in trading_days:
        key = (date.year, date.month)
        if key not in monthly_groups:
            monthly_groups[key] = []
        monthly_groups[key].append(date)
    
    for (year, month), month_dates in monthly_groups.items():
        if len(month_dates) > 10:
            contribution_dates.append(month_dates[0])
    
    # Market times
    entry_time = time(15, 59, 45)
    exit_time = time(15, 59, 30)
    
    # Track current position
    current_position = None
    trades_taken = 0
    trades_skipped = 0
    
    # Process each trading day
    for day_idx, current_date in enumerate(trading_days[20:], 20):
        day_data = df[df['date'] == current_date].copy()
        
        if day_data.empty:
            continue
        
        # Add monthly contribution
        if current_date in contribution_dates:
            balance += 100.0
            total_contributions += 100.0
        
        # Get historical data up to current date for AI analysis
        historical_data = df_with_indicators[df_with_indicators['date'] <= current_date].copy()
        
        # Step 1: Close existing position if exists
        if current_position is not None:
            # Check if stopped out during the day
            stop_hit, stop_price, stop_time = check_stop_loss_hit(
                day_data, current_position['entry_price'], current_position['entry_time'], 
                current_position['direction'], stop_loss_pct
            )
            
            if stop_hit:
                exit_price = stop_price
            else:
                exit_price = get_price_at_time(day_data, exit_time, 'closePrice')
                if exit_price is None:
                    exit_price = current_position['entry_price']
            
            # Calculate P&L
            if current_position['direction'] == 'long':
                gross_return = (exit_price - current_position['entry_price']) / current_position['entry_price']
            else:
                gross_return = (current_position['entry_price'] - exit_price) / current_position['entry_price']
            
            gross_pnl = current_position['position_size'] * gross_return
            trade_costs = calculate_trading_costs(current_position['position_size'], current_position['direction'], is_overnight=True)
            net_pnl = gross_pnl - trade_costs
            
            balance += net_pnl
            total_fees += trade_costs
            
            if net_pnl > 0:
                winning_trades += 1
            else:
                losing_trades += 1
            
            current_position = None
        
        # Step 2: AI Analysis for new position
        ai_analysis = analyze_market_conditions(day_data, historical_data, current_date)
        
        # Step 3: Decision to trade based on AI analysis and confidence threshold
        if (ai_analysis['trade_signal'] in ['buy', 'sell'] and 
            ai_analysis['confidence'] > confidence_threshold):
            
            # AI is confident enough - take the trade
            entry_price = get_price_at_time(day_data, entry_time, 'closePrice')
            if entry_price is None:
                trades_skipped += 1
                continue
            
            # Calculate position size (99% investment)
            investment_amount = balance * 0.99
            position_size = investment_amount * leverage
            
            # Create new position
            current_position = {
                'entry_date': current_date,
                'entry_time': entry_time,
                'direction': ai_analysis['direction'],
                'entry_price': entry_price,
                'position_size': position_size,
                'leverage': leverage
            }
            
            trades_taken += 1
        else:
            trades_skipped += 1
        
        # Track drawdown
        peak_balance = max(peak_balance, balance)
        current_drawdown = (peak_balance - balance) / peak_balance
        max_drawdown = max(max_drawdown, current_drawdown)
    
    # Close final position if exists
    if current_position is not None:
        trade_costs = calculate_trading_costs(current_position['position_size'], current_position['direction'], is_overnight=True)
        balance -= trade_costs
        total_fees += trade_costs
    
    # Calculate final statistics
    total_invested = 100.0 + total_contributions
    trading_return = (balance - total_invested) / total_invested * 100
    win_rate = winning_trades / (winning_trades + losing_trades) * 100 if (winning_trades + losing_trades) > 0 else 0
    
    # Calculate annualized return
    if len(trading_days) > 20:
        days_elapsed = (trading_days[-1] - trading_days[20]).days
        years = days_elapsed / 365.25
        annualized_return = ((balance / total_invested) ** (1/years) - 1) * 100 if years > 0 and balance > 0 else -100
    else:
        annualized_return = 0
    
    trade_selectivity = trades_taken / (trades_taken + trades_skipped) * 100 if (trades_taken + trades_skipped) > 0 else 0
    
    return {
        'strategy_type': 'hybrid_intelligent',
        'leverage': leverage,
        'stop_loss_pct': stop_loss_pct,
        'confidence_threshold': confidence_threshold,
        'final_balance': balance,
        'total_invested': total_invested,
        'trading_return': trading_return,
        'annualized_return': annualized_return,
        'win_rate': win_rate,
        'total_fees': total_fees,
        'winning_trades': winning_trades,
        'losing_trades': losing_trades,
        'max_drawdown': max_drawdown * 100,
        'total_trades': winning_trades + losing_trades,
        'trades_taken': trades_taken,
        'trades_skipped': trades_skipped,
        'trade_selectivity': trade_selectivity
    }

def run_daily_trading_strategy(df, leverage, stop_loss_pct):
    """Daily trading strategy (trade every day)"""
    
    # Get all trading days
    all_dates = sorted(df['date'].unique())
    trading_days = [date for date in all_dates if is_trading_day(date)]
    
    if len(trading_days) < 5:
        return None
    
    # Trading parameters
    balance = 100.0
    total_contributions = 0
    total_fees = 0
    winning_trades = 0
    losing_trades = 0
    max_drawdown = 0
    peak_balance = 100.0
    
    # Monthly contribution dates
    contribution_dates = []
    monthly_groups = {}
    for date in trading_days:
        key = (date.year, date.month)
        if key not in monthly_groups:
            monthly_groups[key] = []
        monthly_groups[key].append(date)
    
    for (year, month), month_dates in monthly_groups.items():
        if len(month_dates) > 10:
            contribution_dates.append(month_dates[0])
    
    # Market times
    entry_time = time(15, 59, 45)
    exit_time = time(15, 59, 30)
    
    # Process each trading day
    for current_date in trading_days:
        day_data = df[df['date'] == current_date].copy()
        
        if day_data.empty:
            continue
        
        # Add monthly contribution
        if current_date in contribution_dates:
            balance += 100.0
            total_contributions += 100.0
        
        # Get entry price
        entry_price = get_price_at_time(day_data, entry_time, 'closePrice')
        if entry_price is None:
            continue
        
        # Calculate position size (99% investment)
        investment_amount = balance * 0.99
        position_size = investment_amount * leverage
        
        # Check if stopped out during the day
        stop_hit, stop_price, stop_time = check_stop_loss_hit(
            day_data, entry_price, entry_time, 'long', stop_loss_pct
        )
        
        if stop_hit:
            exit_price = stop_price
        else:
            exit_price = get_price_at_time(day_data, exit_time, 'closePrice')
            if exit_price is None:
                exit_price = entry_price
        
        # Calculate P&L
        gross_return = (exit_price - entry_price) / entry_price
        gross_pnl = position_size * gross_return
        trade_costs = calculate_trading_costs(position_size, 'long', is_overnight=True)
        net_pnl = gross_pnl - trade_costs
        
        balance += net_pnl
        total_fees += trade_costs
        
        if net_pnl > 0:
            winning_trades += 1
        else:
            losing_trades += 1
        
        # Track drawdown
        peak_balance = max(peak_balance, balance)
        current_drawdown = (peak_balance - balance) / peak_balance
        max_drawdown = max(max_drawdown, current_drawdown)
    
    # Calculate final statistics
    total_invested = 100.0 + total_contributions
    trading_return = (balance - total_invested) / total_invested * 100
    win_rate = winning_trades / (winning_trades + losing_trades) * 100 if (winning_trades + losing_trades) > 0 else 0
    
    # Calculate annualized return
    if len(trading_days) > 5:
        days_elapsed = (trading_days[-1] - trading_days[0]).days
        years = days_elapsed / 365.25
        annualized_return = ((balance / total_invested) ** (1/years) - 1) * 100 if years > 0 and balance > 0 else -100
    else:
        annualized_return = 0
    
    return {
        'strategy_type': 'daily_trading',
        'leverage': leverage,
        'stop_loss_pct': stop_loss_pct,
        'confidence_threshold': 0.0,
        'final_balance': balance,
        'total_invested': total_invested,
        'trading_return': trading_return,
        'annualized_return': annualized_return,
        'win_rate': win_rate,
        'total_fees': total_fees,
        'winning_trades': winning_trades,
        'losing_trades': losing_trades,
        'max_drawdown': max_drawdown * 100,
        'total_trades': winning_trades + losing_trades,
        'trades_taken': winning_trades + losing_trades,
        'trades_skipped': 0,
        'trade_selectivity': 100.0
    }

def calculate_buy_and_hold(df):
    """Calculate buy and hold performance"""
    
    # Get all trading days
    all_dates = sorted(df['date'].unique())
    trading_days = [date for date in all_dates if is_trading_day(date)]
    
    if len(trading_days) < 5:
        return None
    
    # Get first and last prices
    first_day_data = df[df['date'] == trading_days[0]]
    last_day_data = df[df['date'] == trading_days[-1]]
    
    if first_day_data.empty or last_day_data.empty:
        return None
    
    first_price = first_day_data['closePrice'].iloc[0]
    last_price = last_day_data['closePrice'].iloc[-1]
    
    # Calculate monthly contributions
    monthly_groups = {}
    for date in trading_days:
        key = (date.year, date.month)
        if key not in monthly_groups:
            monthly_groups[key] = []
        monthly_groups[key].append(date)
    
    contribution_dates = []
    for (year, month), month_dates in monthly_groups.items():
        if len(month_dates) > 10:
            contribution_dates.append(month_dates[0])
    
    total_contributions = len(contribution_dates) * 100.0
    total_invested = 100.0 + total_contributions
    
    # Calculate shares bought
    shares = total_invested / first_price
    final_value = shares * last_price
    
    buy_hold_return = (final_value - total_invested) / total_invested * 100
    
    # Calculate annualized return
    days_elapsed = (trading_days[-1] - trading_days[0]).days
    years = days_elapsed / 365.25
    annualized_return = ((final_value / total_invested) ** (1/years) - 1) * 100 if years > 0 else 0
    
    return {
        'strategy_type': 'buy_and_hold',
        'leverage': 1.0,
        'stop_loss_pct': 0.0,
        'confidence_threshold': 0.0,
        'final_balance': final_value,
        'total_invested': total_invested,
        'trading_return': buy_hold_return,
        'annualized_return': annualized_return,
        'win_rate': 100.0 if buy_hold_return > 0 else 0.0,
        'total_fees': 0.0,
        'winning_trades': 1 if buy_hold_return > 0 else 0,
        'losing_trades': 0 if buy_hold_return > 0 else 1,
        'max_drawdown': 0.0,
        'total_trades': 1,
        'trades_taken': 1,
        'trades_skipped': 0,
        'trade_selectivity': 100.0
    }

def load_tecl_data():
    """Load TECL data"""
    file_path = "alphavantage_data/TECL_5m_alphavantage.csv"
    
    try:
        df = pd.read_csv(file_path)
        df['snapshotTime'] = pd.to_datetime(df['snapshotTime'])
        df['date'] = df['snapshotTime'].dt.date
        df['time'] = df['snapshotTime'].dt.time
        df['day_of_week'] = df['snapshotTime'].dt.day_name()
        return df.sort_values('snapshotTime')
    except Exception as e:
        print(f"Error loading TECL data: {e}")
        return None

def main():
    print(f"\n🚀 Loading TECL data...")
    
    df = load_tecl_data()
    if df is None:
        print("❌ Failed to load TECL data")
        return
    
    print(f"✅ Loaded {len(df):,} records of TECL data")
    print(f"📅 Date range: {df['date'].min()} to {df['date'].max()}")
    
    # Define focused parameter space
    leverages = [2.0, 3.0, 4.0]
    stop_losses = [2.0, 3.0, 4.0]
    confidence_thresholds = [0.30, 0.40, 0.50]
    
    print(f"\n📊 FOCUSED OPTIMIZATION PARAMETERS:")
    print(f"  Leverages: {leverages}")
    print(f"  Stop Losses: {stop_losses}%")
    print(f"  AI Confidence: {[int(c*100) for c in confidence_thresholds]}%")
    print(f"  Investment: 99% (maximum growth)")
    print("-" * 80)
    
    # Run all combinations
    results = []
    total_combinations = len(leverages) * len(stop_losses) * len(confidence_thresholds) + len(leverages) * len(stop_losses) + 1
    
    print(f"🚀 Running {total_combinations} optimizations...")
    
    # 1. Hybrid Intelligent Strategy
    for leverage in leverages:
        for stop_loss in stop_losses:
            for confidence in confidence_thresholds:
                print(f"  Testing Hybrid: {leverage}x leverage, {stop_loss}% stop, {int(confidence*100)}% confidence...")
                result = run_hybrid_intelligent_strategy(df, leverage, stop_loss, confidence)
                if result:
                    results.append(result)
    
    # 2. Daily Trading Strategy
    for leverage in leverages:
        for stop_loss in stop_losses:
            print(f"  Testing Daily: {leverage}x leverage, {stop_loss}% stop...")
            result = run_daily_trading_strategy(df, leverage, stop_loss)
            if result:
                results.append(result)
    
    # 3. Buy and Hold
    print(f"  Testing Buy & Hold...")
    buy_hold_result = calculate_buy_and_hold(df)
    if buy_hold_result:
        results.append(buy_hold_result)
    
    if not results:
        print("❌ No successful optimizations")
        return
    
    print(f"\n✅ Optimization complete! {len(results)} results")
    
    # Sort results by trading return
    results.sort(key=lambda x: x['trading_return'], reverse=True)
    
    # Display results
    print(f"\n" + "="*100)
    print(f"FOCUSED STRATEGY OPTIMIZATION RESULTS")
    print(f"="*100)
    
    print(f"📊 ALL STRATEGIES (Top 15):")
    print(f"{'Rank':<4} {'Strategy':<16} {'Lev':<4} {'Stop':<5} {'Conf%':<5} {'Return':<8} {'Annual':<8} {'Win%':<6} {'Trades':<7} {'Select%':<7}")
    print("-" * 100)
    
    for i, result in enumerate(results[:15], 1):
        strategy_short = result['strategy_type'][:12]
        print(f"{i:<4} {strategy_short:<16} {result['leverage']:<4.1f} {result['stop_loss_pct']:<5.1f} "
              f"{result['confidence_threshold']*100:<5.0f} {result['trading_return']:+6.1f}% {result['annualized_return']:+6.1f}% "
              f"{result['win_rate']:5.1f}% {result['total_trades']:<7} {result['trade_selectivity']:6.1f}%")
    
    # Strategy type analysis
    hybrid_results = [r for r in results if r['strategy_type'] == 'hybrid_intelligent']
    daily_results = [r for r in results if r['strategy_type'] == 'daily_trading']
    buy_hold_results = [r for r in results if r['strategy_type'] == 'buy_and_hold']
    
    print(f"\n📈 STRATEGY TYPE COMPARISON:")
    print(f"Strategy Type        | Count | Avg Return | Best Return | Profitable %")
    print("-" * 75)
    
    for strategy_name, strategy_results in [
        ('Hybrid Intelligent', hybrid_results),
        ('Daily Trading', daily_results),
        ('Buy & Hold', buy_hold_results)
    ]:
        if strategy_results:
            count = len(strategy_results)
            avg_return = sum(r['trading_return'] for r in strategy_results) / count
            best_return = max(r['trading_return'] for r in strategy_results)
            profitable = len([r for r in strategy_results if r['trading_return'] > 0])
            profitable_pct = profitable / count * 100
            
            print(f"{strategy_name:<20} | {count:<5} | {avg_return:+8.1f}% | {best_return:+9.1f}% | {profitable_pct:8.1f}%")
    
    # Best strategy details
    best_strategy = results[0]
    print(f"\n🏆 OPTIMAL STRATEGY DETAILS:")
    print(f"  Strategy: {best_strategy['strategy_type'].title()}")
    print(f"  Leverage: {best_strategy['leverage']:.1f}x")
    print(f"  Stop Loss: {best_strategy['stop_loss_pct']:.1f}%")
    if best_strategy['strategy_type'] == 'hybrid_intelligent':
        print(f"  AI Confidence: {best_strategy['confidence_threshold']*100:.0f}%")
        print(f"  Trade Selectivity: {best_strategy['trade_selectivity']:.1f}%")
    print(f"  Final Balance: ${best_strategy['final_balance']:,.2f}")
    print(f"  Total Invested: ${best_strategy['total_invested']:,.2f}")
    print(f"  Trading Return: {best_strategy['trading_return']:+.1f}%")
    print(f"  Annualized Return: {best_strategy['annualized_return']:+.1f}%")
    print(f"  Win Rate: {best_strategy['win_rate']:.1f}%")
    print(f"  Total Trades: {best_strategy['total_trades']}")
    print(f"  Max Drawdown: {best_strategy['max_drawdown']:.1f}%")
    print(f"  Total Fees: ${best_strategy['total_fees']:,.2f}")
    
    # Save results
    results_df = pd.DataFrame(results)
    results_df.to_csv('focused_optimization_results.csv', index=False)
    print(f"\n📊 Results saved to: focused_optimization_results.csv")
    
    print(f"\n🎯 Focused Strategy Optimization Complete!")
    print(f"🏆 Optimal strategy achieves {best_strategy['trading_return']:+.1f}% returns!")

if __name__ == "__main__":
    main()
