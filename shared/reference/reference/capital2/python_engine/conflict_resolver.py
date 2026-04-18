"""
Conflict Resolution Engine

Handles conflicts when multiple strategies signal different directions at the same time.
Supports single-metric and multi-metric weighted resolution modes.
"""

from typing import List, Dict, Optional, Tuple
from enum import Enum
import json

class ResolutionMode(Enum):
    """Conflict resolution modes"""
    # Single metric modes
    HIGHEST_PROFITABILITY = "highest_profitability"
    HIGHEST_WIN_RATE = "highest_win_rate"
    BEST_SHARPE_RATIO = "best_sharpe_ratio"
    MIN_DRAWDOWN = "min_drawdown"
    BEST_RISK_REWARD = "best_risk_reward"
    
    # Combined modes
    WEIGHTED_SCORE = "weighted_score"
    
    # Other modes
    VOTING = "voting"
    CONSERVATIVE = "conservative"  # All must agree
    AGGRESSIVE = "aggressive"      # Any signal triggers


class StrategySignal:
    """Represents a signal from a single strategy"""
    def __init__(
        self,
        strategy_id: str,
        strategy_name: str,
        direction: str,  # 'long', 'short', or 'neutral'
        timestamp: str,
        
        # Performance metrics
        profitability: float = 0.0,
        win_rate: float = 0.0,
        sharpe_ratio: float = 0.0,
        max_drawdown: float = 0.0,
        risk_reward_ratio: float = 0.0,
        
        # Additional context
        confidence: float = 1.0,
        metadata: Optional[Dict] = None
    ):
        self.strategy_id = strategy_id
        self.strategy_name = strategy_name
        self.direction = direction
        self.timestamp = timestamp
        
        self.profitability = profitability
        self.win_rate = win_rate
        self.sharpe_ratio = sharpe_ratio
        self.max_drawdown = max_drawdown
        self.risk_reward_ratio = risk_reward_ratio
        
        self.confidence = confidence
        self.metadata = metadata or {}
    
    def to_dict(self) -> Dict:
        """Convert to dictionary"""
        return {
            'strategy_id': self.strategy_id,
            'strategy_name': self.strategy_name,
            'direction': self.direction,
            'timestamp': self.timestamp,
            'profitability': self.profitability,
            'win_rate': self.win_rate,
            'sharpe_ratio': self.sharpe_ratio,
            'max_drawdown': self.max_drawdown,
            'risk_reward_ratio': self.risk_reward_ratio,
            'confidence': self.confidence,
            'metadata': self.metadata,
        }


class ConflictResolution:
    """Result of conflict resolution"""
    def __init__(
        self,
        final_direction: str,
        winning_strategy: Optional[StrategySignal],
        all_signals: List[StrategySignal],
        resolution_mode: ResolutionMode,
        conflict_detected: bool,
        resolution_details: Dict
    ):
        self.final_direction = final_direction
        self.winning_strategy = winning_strategy
        self.all_signals = all_signals
        self.resolution_mode = resolution_mode
        self.conflict_detected = conflict_detected
        self.resolution_details = resolution_details
    
    def to_dict(self) -> Dict:
        """Convert to dictionary"""
        return {
            'final_direction': self.final_direction,
            'winning_strategy': self.winning_strategy.to_dict() if self.winning_strategy else None,
            'all_signals': [s.to_dict() for s in self.all_signals],
            'resolution_mode': self.resolution_mode.value,
            'conflict_detected': self.conflict_detected,
            'resolution_details': self.resolution_details,
        }


class ConflictResolver:
    """Resolves conflicts between multiple strategy signals"""
    
    def __init__(
        self,
        resolution_mode: ResolutionMode = ResolutionMode.VOTING,
        metric_weights: Optional[Dict[str, float]] = None
    ):
        """
        Initialize conflict resolver
        
        Args:
            resolution_mode: How to resolve conflicts
            metric_weights: For WEIGHTED_SCORE mode, dict of metric -> weight
                          Example: {'win_rate': 0.4, 'sharpe_ratio': 0.3, 'max_drawdown': 0.3}
        """
        self.resolution_mode = resolution_mode
        self.metric_weights = metric_weights or {}
        
        # Validate weights sum to 1.0 for weighted mode
        if resolution_mode == ResolutionMode.WEIGHTED_SCORE:
            if not self.metric_weights:
                raise ValueError("metric_weights required for WEIGHTED_SCORE mode")
            
            total_weight = sum(self.metric_weights.values())
            if abs(total_weight - 1.0) > 0.01:
                raise ValueError(f"Metric weights must sum to 1.0, got {total_weight}")
    
    def resolve(self, signals: List[StrategySignal]) -> ConflictResolution:
        """
        Resolve conflicts between multiple signals
        
        Args:
            signals: List of strategy signals
            
        Returns:
            ConflictResolution with final direction and details
        """
        if not signals:
            return ConflictResolution(
                final_direction='neutral',
                winning_strategy=None,
                all_signals=[],
                resolution_mode=self.resolution_mode,
                conflict_detected=False,
                resolution_details={'reason': 'No signals provided'}
            )
        
        # Check if there's a conflict
        directions = set(s.direction for s in signals)
        conflict_detected = len(directions) > 1
        
        # If no conflict, just return the unanimous direction
        if not conflict_detected:
            return ConflictResolution(
                final_direction=signals[0].direction,
                winning_strategy=signals[0],
                all_signals=signals,
                resolution_mode=self.resolution_mode,
                conflict_detected=False,
                resolution_details={'reason': 'All strategies agree'}
            )
        
        # Resolve conflict based on mode
        if self.resolution_mode == ResolutionMode.HIGHEST_PROFITABILITY:
            return self._resolve_by_single_metric(signals, 'profitability', maximize=True)
        
        elif self.resolution_mode == ResolutionMode.HIGHEST_WIN_RATE:
            return self._resolve_by_single_metric(signals, 'win_rate', maximize=True)
        
        elif self.resolution_mode == ResolutionMode.BEST_SHARPE_RATIO:
            return self._resolve_by_single_metric(signals, 'sharpe_ratio', maximize=True)
        
        elif self.resolution_mode == ResolutionMode.MIN_DRAWDOWN:
            return self._resolve_by_single_metric(signals, 'max_drawdown', maximize=False)
        
        elif self.resolution_mode == ResolutionMode.BEST_RISK_REWARD:
            return self._resolve_by_single_metric(signals, 'risk_reward_ratio', maximize=True)
        
        elif self.resolution_mode == ResolutionMode.WEIGHTED_SCORE:
            return self._resolve_by_weighted_score(signals)
        
        elif self.resolution_mode == ResolutionMode.VOTING:
            return self._resolve_by_voting(signals)
        
        elif self.resolution_mode == ResolutionMode.CONSERVATIVE:
            return self._resolve_conservative(signals)
        
        elif self.resolution_mode == ResolutionMode.AGGRESSIVE:
            return self._resolve_aggressive(signals)
        
        else:
            raise ValueError(f"Unknown resolution mode: {self.resolution_mode}")
    
    def _resolve_by_single_metric(
        self,
        signals: List[StrategySignal],
        metric_name: str,
        maximize: bool = True
    ) -> ConflictResolution:
        """Resolve by choosing strategy with best single metric"""
        
        # Sort by metric
        sorted_signals = sorted(
            signals,
            key=lambda s: getattr(s, metric_name),
            reverse=maximize
        )
        
        winner = sorted_signals[0]
        metric_value = getattr(winner, metric_name)
        
        return ConflictResolution(
            final_direction=winner.direction,
            winning_strategy=winner,
            all_signals=signals,
            resolution_mode=self.resolution_mode,
            conflict_detected=True,
            resolution_details={
                'metric': metric_name,
                'metric_value': metric_value,
                'maximize': maximize,
                'all_metrics': {s.strategy_id: getattr(s, metric_name) for s in signals}
            }
        )
    
    def _resolve_by_weighted_score(self, signals: List[StrategySignal]) -> ConflictResolution:
        """Resolve by calculating weighted composite score"""
        
        scores = {}
        score_breakdown = {}
        
        for signal in signals:
            score = 0.0
            breakdown = {}
            
            for metric, weight in self.metric_weights.items():
                value = getattr(signal, metric, 0.0)
                
                # Normalize max_drawdown (lower is better, so invert)
                if metric == 'max_drawdown':
                    value = 1.0 - min(value / 100.0, 1.0)  # Convert to 0-1 scale, inverted
                
                contribution = value * weight
                score += contribution
                breakdown[metric] = {'value': value, 'weight': weight, 'contribution': contribution}
            
            scores[signal.strategy_id] = score
            score_breakdown[signal.strategy_id] = breakdown
        
        # Find winner
        winner_id = max(scores, key=scores.get)
        winner = next(s for s in signals if s.strategy_id == winner_id)
        
        return ConflictResolution(
            final_direction=winner.direction,
            winning_strategy=winner,
            all_signals=signals,
            resolution_mode=self.resolution_mode,
            conflict_detected=True,
            resolution_details={
                'scores': scores,
                'score_breakdown': score_breakdown,
                'weights': self.metric_weights
            }
        )
    
    def _resolve_by_voting(self, signals: List[StrategySignal]) -> ConflictResolution:
        """Resolve by majority vote"""
        
        votes = {}
        for signal in signals:
            votes[signal.direction] = votes.get(signal.direction, 0) + 1
        
        # Find direction with most votes
        winner_direction = max(votes, key=votes.get)
        winner_signal = next(s for s in signals if s.direction == winner_direction)
        
        return ConflictResolution(
            final_direction=winner_direction,
            winning_strategy=winner_signal,
            all_signals=signals,
            resolution_mode=self.resolution_mode,
            conflict_detected=True,
            resolution_details={
                'votes': votes,
                'winner_votes': votes[winner_direction],
                'total_votes': len(signals)
            }
        )
    
    def _resolve_conservative(self, signals: List[StrategySignal]) -> ConflictResolution:
        """Conservative: only trade if all agree"""
        
        directions = set(s.direction for s in signals)
        
        if len(directions) == 1:
            direction = list(directions)[0]
            return ConflictResolution(
                final_direction=direction,
                winning_strategy=signals[0],
                all_signals=signals,
                resolution_mode=self.resolution_mode,
                conflict_detected=False,
                resolution_details={'reason': 'All strategies agree'}
            )
        else:
            return ConflictResolution(
                final_direction='neutral',
                winning_strategy=None,
                all_signals=signals,
                resolution_mode=self.resolution_mode,
                conflict_detected=True,
                resolution_details={
                    'reason': 'Strategies disagree, conservative mode chooses neutral',
                    'directions': list(directions)
                }
            )
    
    def _resolve_aggressive(self, signals: List[StrategySignal]) -> ConflictResolution:
        """Aggressive: trade on any signal (first non-neutral wins)"""
        
        # Find first non-neutral signal
        for signal in signals:
            if signal.direction != 'neutral':
                return ConflictResolution(
                    final_direction=signal.direction,
                    winning_strategy=signal,
                    all_signals=signals,
                    resolution_mode=self.resolution_mode,
                    conflict_detected=True,
                    resolution_details={
                        'reason': 'Aggressive mode: first non-neutral signal wins',
                        'first_signal': signal.strategy_id
                    }
                )
        
        # All neutral
        return ConflictResolution(
            final_direction='neutral',
            winning_strategy=signals[0],
            all_signals=signals,
            resolution_mode=self.resolution_mode,
            conflict_detected=False,
            resolution_details={'reason': 'All strategies neutral'}
        )


# =============================================================================
# SIMPLE CONFLICT RESOLUTION (for brain/DNA strand conflicts)
# =============================================================================
# All signals are BUY (same direction), we just need to pick the best one
# Based on performance metrics from historical backtest results

def resolve_buy_signals(
    buy_signals: List[Dict],
    mode: str = 'sharpeRatio'
) -> Dict:
    """
    Simple conflict resolution for brain/DNA calculations.
    
    All signals are BUY - we pick the winner based on historical performance.
    This is the SINGLE implementation used by:
    - TypeScript brain.ts (via conflict_bridge.ts)
    - TypeScript dna_brain_accumulator.ts (via conflict_bridge.ts)
    
    Args:
        buy_signals: List of dictionaries with:
            - index: int - Index in original test list
            - indicatorName: str - Name of indicator
            - sharpe: float - Sharpe ratio (optional)
            - totalReturn: float - Total return % (optional)
            - winRate: float - Win rate % (optional)
            - maxDrawdown: float - Max drawdown % (optional)
            - leverage: float (optional)
            - stopLoss: float (optional)
            - epic: str (optional)
            - timeframe: str (optional)
            
        mode: Conflict resolution mode - one of:
            - 'sharpeRatio' or 'sharpe' or 'highest_sharpe' - Best Sharpe ratio (default)
            - 'profitability' or 'return' or 'totalReturn' - Best total return
            - 'winRate' or 'win_rate' - Best win rate
            - 'maxDrawdown' or 'drawdown' or 'min_drawdown' - Lowest drawdown
            - 'first_signal' - First signal wins (by index)
            
    Returns:
        {
            'winnerIndex': int - Index of winning signal in buy_signals list
            'originalIndex': int - Original index in test list
            'winner': dict - Full winner signal data
            'mode': str - Resolution mode used
            'hadConflict': bool - True if multiple signals competed
            'reason': str - Human-readable explanation
        }
    """
    if not buy_signals:
        return {
            'winnerIndex': -1,
            'originalIndex': -1,
            'winner': None,
            'mode': mode,
            'hadConflict': False,
            'reason': 'No BUY signals to resolve'
        }
    
    if len(buy_signals) == 1:
        return {
            'winnerIndex': 0,
            'originalIndex': buy_signals[0].get('index', 0),
            'winner': buy_signals[0],
            'mode': mode,
            'hadConflict': False,
            'reason': f"Single BUY signal: {buy_signals[0].get('indicatorName', 'unknown')}"
        }
    
    # Multiple BUY signals - resolve conflict
    mode_lower = mode.lower().replace('_', '')
    
    if mode_lower in ['sharperatio', 'sharpe', 'highestsharpe', 'bestsharpe']:
        # Best Sharpe ratio (highest wins)
        winner_idx = max(range(len(buy_signals)), 
                        key=lambda i: buy_signals[i].get('sharpe', 0) or 0)
        winner = buy_signals[winner_idx]
        reason = f"Best Sharpe Ratio ({winner.get('sharpe', 0):.2f})"
        
    elif mode_lower in ['profitability', 'return', 'totalreturn', 'profit', 'highestreturn']:
        # Best total return (highest wins)
        winner_idx = max(range(len(buy_signals)), 
                        key=lambda i: buy_signals[i].get('totalReturn', 0) or 0)
        winner = buy_signals[winner_idx]
        reason = f"Best Return ({winner.get('totalReturn', 0):.2f}%)"
        
    elif mode_lower in ['winrate', 'win', 'highestwinrate']:
        # Best win rate (highest wins)
        winner_idx = max(range(len(buy_signals)), 
                        key=lambda i: buy_signals[i].get('winRate', 0) or 0)
        winner = buy_signals[winner_idx]
        reason = f"Best Win Rate ({winner.get('winRate', 0):.1f}%)"
        
    elif mode_lower in ['maxdrawdown', 'drawdown', 'mindrawdown', 'lowestdrawdown']:
        # Lowest max drawdown (lowest absolute value wins)
        winner_idx = min(range(len(buy_signals)), 
                        key=lambda i: abs(buy_signals[i].get('maxDrawdown', 100) or 100))
        winner = buy_signals[winner_idx]
        reason = f"Lowest Drawdown ({abs(winner.get('maxDrawdown', 0)):.1f}%)"
        
    elif mode_lower in ['firstsignal', 'first']:
        # First signal wins (by index)
        winner_idx = min(range(len(buy_signals)), 
                        key=lambda i: buy_signals[i].get('index', i))
        winner = buy_signals[winner_idx]
        reason = f"First Signal: {winner.get('indicatorName', 'unknown')}"
        
    else:
        # Default to Sharpe ratio
        winner_idx = max(range(len(buy_signals)), 
                        key=lambda i: buy_signals[i].get('sharpe', 0) or 0)
        winner = buy_signals[winner_idx]
        reason = f"Default (Sharpe): {winner.get('sharpe', 0):.2f}"
    
    return {
        'winnerIndex': winner_idx,
        'originalIndex': winner.get('index', winner_idx),
        'winner': winner,
        'mode': mode,
        'hadConflict': True,
        'reason': reason,
        'competingSignals': [
            {
                'index': s.get('index', i),
                'indicator': s.get('indicatorName', 'unknown'),
                'sharpe': s.get('sharpe'),
                'totalReturn': s.get('totalReturn'),
                'winRate': s.get('winRate'),
                'maxDrawdown': s.get('maxDrawdown'),
            }
            for i, s in enumerate(buy_signals)
        ]
    }


def main_cli():
    """
    CLI interface for conflict resolution.
    
    Called by TypeScript via conflict_bridge.ts:
        python conflict_resolver.py '{"signals": [...], "mode": "sharpeRatio"}'
    
    Input JSON:
        {
            "signals": [
                {"index": 0, "indicatorName": "rsi_oversold", "sharpe": 1.5, ...},
                {"index": 1, "indicatorName": "macd_bullish", "sharpe": 2.0, ...}
            ],
            "mode": "sharpeRatio"
        }
    
    Output (printed as RESULT:{json}):
        {
            "winnerIndex": 1,
            "originalIndex": 1,
            "winner": {...},
            "mode": "sharpeRatio",
            "hadConflict": true,
            "reason": "Best Sharpe Ratio (2.00)"
        }
    """
    import sys
    
    if len(sys.argv) < 2:
        print("ERROR: Missing JSON config argument", file=sys.stderr)
        print("Usage: python conflict_resolver.py '{\"signals\": [...], \"mode\": \"sharpeRatio\"}'", file=sys.stderr)
        sys.exit(1)
    
    try:
        config = json.loads(sys.argv[1])
        signals = config.get('signals', [])
        mode = config.get('mode', 'sharpeRatio')
        
        result = resolve_buy_signals(signals, mode)
        
        print(f"RESULT:{json.dumps(result)}")
        sys.exit(0)
        
    except Exception as e:
        print(f"ERROR: {str(e)}", file=sys.stderr)
        import traceback
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)


# Helper function for testing
if __name__ == '__main__':
    import sys
    
    # If command line args, run CLI mode
    if len(sys.argv) > 1:
        main_cli()
    else:
        # Test conflict resolution
        signals = [
            StrategySignal(
                strategy_id='rsi_oversold',
                strategy_name='RSI Oversold',
                direction='long',
                timestamp='2025-01-01 15:59:45',
                profitability=1500.0,
                win_rate=0.65,
                sharpe_ratio=1.8,
                max_drawdown=15.0,
                risk_reward_ratio=2.5
            ),
            StrategySignal(
                strategy_id='macd_bearish',
                strategy_name='MACD Bearish Cross',
                direction='short',
                timestamp='2025-01-01 15:59:45',
                profitability=1200.0,
                win_rate=0.60,
                sharpe_ratio=1.5,
                max_drawdown=20.0,
                risk_reward_ratio=2.0
            ),
            StrategySignal(
                strategy_id='bb_squeeze',
                strategy_name='Bollinger Squeeze',
                direction='long',
                timestamp='2025-01-01 15:59:45',
                profitability=1800.0,
                win_rate=0.70,
                sharpe_ratio=2.0,
                max_drawdown=12.0,
                risk_reward_ratio=3.0
            ),
        ]
        
        # Test voting mode
        resolver = ConflictResolver(resolution_mode=ResolutionMode.VOTING)
        result = resolver.resolve(signals)
        print("Voting Mode:")
        print(json.dumps(result.to_dict(), indent=2))
        print()
        
        # Test weighted score mode
        resolver = ConflictResolver(
            resolution_mode=ResolutionMode.WEIGHTED_SCORE,
            metric_weights={
                'win_rate': 0.4,
                'sharpe_ratio': 0.3,
                'max_drawdown': 0.3
            }
        )
        result = resolver.resolve(signals)
        print("Weighted Score Mode:")
        print(json.dumps(result.to_dict(), indent=2))

