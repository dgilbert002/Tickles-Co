#!/usr/bin/env python3
"""
Advanced Batch Optimizer with Multiple Sampling Strategies

Supports:
- Pure Random sampling
- Grid Search (systematic)
- Simulated Annealing (hot/cold optimization)
- Genetic Algorithm (evolutionary)
- Bayesian Optimization (smart sampling)
"""

import random
import numpy as np
from typing import Dict, List, Any, Optional, Callable
from dataclasses import dataclass
import json
import multiprocessing as mp
import traceback
import psutil
import sys

@dataclass
class OptimizationConfig:
    """Configuration for optimization run"""
    strategy: str  # 'random', 'grid', 'annealing', 'genetic', 'bayesian'
    num_tests: int
    
    # Stop conditions
    max_drawdown_threshold: Optional[float] = None  # Abort if DD > this (e.g., 50%)
    min_win_rate_threshold: Optional[float] = None  # Abort if WR < this (e.g., 40%)
    min_sharpe_threshold: Optional[float] = None
    max_loss_streak: Optional[int] = None
    
    # Multi-objective weights (for scoring)
    weight_profitability: float = 0.4
    weight_sharpe: float = 0.2
    weight_win_rate: float = 0.2
    weight_drawdown: float = 0.1  # Lower is better
    weight_recovery: float = 0.1
    
    # Strategy-specific params
    annealing_temp_start: float = 1.0
    annealing_temp_end: float = 0.01
    annealing_cooling_rate: float = 0.95
    
    genetic_population_size: int = 20
    genetic_mutation_rate: float = 0.1
    genetic_crossover_rate: float = 0.7
    
    bayesian_n_initial: int = 10  # Random samples before Bayesian kicks in
    
    # Parallel processing
    parallel_cores: int = 1  # Number of CPU cores to use
    
    # Duplicate handling
    rerun_duplicates: bool = False  # If True, ignore duplicate hash check


class ParameterSpace:
    """Defines the parameter space for optimization"""
    
    def __init__(self, param_ranges: Dict[str, Any]):
        """
        param_ranges format:
        {
            'leverage': [2, 3, 4, 5, 10],  # Discrete values
            'stop_loss': {'min': 2.0, 'max': 10.0, 'step': 0.5},  # Continuous range
            'rsi_period': {'min': 7, 'max': 21, 'step': 1},  # Integer range
        }
        """
        self.param_ranges = param_ranges
        self.param_names = list(param_ranges.keys())
        self._all_combinations = None  # Cached for unique sampling
        self._used_indices = set()  # Track which combinations have been used
    
    def _get_all_combinations(self) -> List[Dict[str, Any]]:
        """Get all possible parameter combinations (cached)"""
        import sys
        if self._all_combinations is not None:
            return self._all_combinations
        
        import itertools
        
        print(f"      [DEBUG-PS-2] Building combinations for params: {self.param_names}", file=sys.stderr, flush=True)
        print(f"      [DEBUG-PS-3] param_ranges: {self.param_ranges}", file=sys.stderr, flush=True)
        
        # Build lists of all possible values for each parameter
        param_values = []
        for name in self.param_names:
            spec = self.param_ranges[name]
            print(f"      [DEBUG-PS-4] Processing param '{name}': spec={spec}", file=sys.stderr, flush=True)
            
            if isinstance(spec, list):
                param_values.append(spec)
                print(f"      [DEBUG-PS-5] '{name}' is list with {len(spec)} values", file=sys.stderr, flush=True)
            elif isinstance(spec, dict):
                min_val = spec['min']
                max_val = spec['max']
                step = spec.get('step', 0.1)
                
                # Guard against step=0 which would cause infinite loop
                if step <= 0:
                    print(f"      [WARNING] '{name}' has step={step}, defaulting to single value {min_val}", file=sys.stderr, flush=True)
                    values = [min_val]  # Just use the min value
                elif isinstance(min_val, int):
                    values = list(range(int(min_val), int(max_val) + 1, int(step) if step >= 1 else 1))
                else:
                    values = []
                    val = min_val
                    while val <= max_val + 0.0001:  # Small epsilon for float comparison
                        values.append(round(val, 2))
                        val += step
                
                param_values.append(values)
                print(f"      [DEBUG-PS-6] '{name}' range generated {len(values)} values (min={min_val}, max={max_val}, step={step})", file=sys.stderr, flush=True)
        
        # Calculate total combinations before generating
        total_combos = 1
        for pv in param_values:
            total_combos *= len(pv)
        print(f"      [DEBUG-PS-7] About to generate {total_combos} combinations...", file=sys.stderr, flush=True)
        
        # Generate all combinations
        self._all_combinations = [
            dict(zip(self.param_names, combo))
            for combo in itertools.product(*param_values)
        ]
        
        print(f"      [DEBUG-PS-8] Generated {len(self._all_combinations)} combinations", file=sys.stderr, flush=True)
        return self._all_combinations
    
    def get_total_combinations(self) -> int:
        """Get the total number of unique parameter combinations possible"""
        return len(self._get_all_combinations())
    
    def sample_random(self) -> Dict[str, Any]:
        """Sample random parameters from the space (may produce duplicates)"""
        params = {}
        for name, spec in self.param_ranges.items():
            if isinstance(spec, list):
                # Discrete values
                params[name] = random.choice(spec)
            elif isinstance(spec, dict):
                # Continuous/integer range
                min_val = spec['min']
                max_val = spec['max']
                step = spec.get('step', 0.1)
                
                if isinstance(min_val, int) and isinstance(max_val, int):
                    # Integer range
                    params[name] = random.randint(min_val, max_val)
                else:
                    # Float range
                    num_steps = int((max_val - min_val) / step)
                    params[name] = min_val + random.randint(0, num_steps) * step
        
        return params
    
    def sample_unique(self, count: int) -> List[Dict[str, Any]]:
        """
        Sample unique random parameters from the space.
        Returns up to 'count' unique combinations (or all if fewer available).
        """
        all_combos = self._get_all_combinations()
        total_available = len(all_combos)
        
        # Get indices we haven't used yet
        available_indices = [i for i in range(total_available) if i not in self._used_indices]
        
        # If we need more than available, just use what we have
        sample_count = min(count, len(available_indices))
        
        if sample_count == 0:
            return []
        
        # Randomly select from available indices
        selected_indices = random.sample(available_indices, sample_count)
        
        # Mark as used
        self._used_indices.update(selected_indices)
        
        # Return the selected combinations
        return [all_combos[i] for i in selected_indices]
    
    def reset_used(self):
        """Reset the tracking of used combinations"""
        self._used_indices = set()
    
    def get_remaining_unique(self) -> int:
        """Get how many unique combinations are still available"""
        return len(self._get_all_combinations()) - len(self._used_indices)
    
    def grid_iterator(self):
        """Generate all combinations for grid search"""
        import itertools
        
        # Build lists of all possible values for each parameter
        param_values = []
        for name in self.param_names:
            spec = self.param_ranges[name]
            if isinstance(spec, list):
                param_values.append(spec)
            elif isinstance(spec, dict):
                min_val = spec['min']
                max_val = spec['max']
                step = spec.get('step', 0.1)
                
                if isinstance(min_val, int):
                    values = list(range(min_val, max_val + 1, int(step)))
                else:
                    values = []
                    val = min_val
                    while val <= max_val:
                        values.append(round(val, 2))
                        val += step
                
                param_values.append(values)
        
        # Generate all combinations
        for combination in itertools.product(*param_values):
            yield dict(zip(self.param_names, combination))


class BatchOptimizer:
    """Main optimizer class"""
    
    def __init__(self, config: OptimizationConfig, param_space: ParameterSpace):
        self.config = config
        self.param_space = param_space
        self.results: List[Dict] = []
        self.best_score = float('-inf')
        self.best_params = None
    
    def calculate_score(self, result: Dict) -> float:
        """Calculate multi-objective score for a result"""
        c = self.config
        
        # Normalize metrics to 0-1 range
        profitability = min(result.get('totalReturn', 0) / 1000, 1.0)  # Cap at 1000%
        sharpe = min(max(result.get('sharpeRatio', 0) / 5, 0), 1.0)  # Cap at 5
        win_rate = result.get('winRate', 0) / 100
        drawdown_penalty = 1.0 - min(abs(result.get('maxDrawdown', 0)) / 100, 1.0)
        
        # Recovery factor (profit / max drawdown)
        recovery = 0.5
        if result.get('maxDrawdown', 0) != 0:
            recovery = min(abs(result.get('totalReturn', 0) / result.get('maxDrawdown', 1)), 1.0)
        
        score = (
            c.weight_profitability * profitability +
            c.weight_sharpe * sharpe +
            c.weight_win_rate * win_rate +
            c.weight_drawdown * drawdown_penalty +
            c.weight_recovery * recovery
        )
        
        return score
    
    def should_abort(self, result: Dict) -> bool:
        """Check if test should be aborted based on stop conditions"""
        c = self.config
        
        if c.max_drawdown_threshold and abs(result.get('maxDrawdown', 0)) > c.max_drawdown_threshold:
            return True
        
        if c.min_win_rate_threshold and result.get('winRate', 0) < c.min_win_rate_threshold:
            return True
        
        if c.min_sharpe_threshold and result.get('sharpeRatio', 0) < c.min_sharpe_threshold:
            return True
        
        # TODO: Implement loss streak tracking
        
        return False
    
    def _run_single_test_safe(self, params: Dict, test_func: Callable, test_num: int) -> Optional[Dict]:
        """Run a single test with error handling and logging"""
        try:
            result = test_func(params)
            
            if self.should_abort(result):
                print(f"  ⚠️  Test {test_num} aborted (stop condition met)", file=sys.stderr)
                return None
            
            score = self.calculate_score(result)
            result['score'] = score
            result['params'] = params
            return result
            
        except Exception as e:
            print(f"  ❌ Test {test_num} FAILED: {str(e)}", file=sys.stderr)
            print(f"     Parameters: {json.dumps(params)}", file=sys.stderr)
            print(f"     Error: {traceback.format_exc()}", file=sys.stderr)
            return None
    
    def optimize_random(self, test_func: Callable) -> List[Dict]:
        """Pure random sampling with optional parallel execution"""
        cores = self.config.parallel_cores
        
        # Log system resources
        try:
            mem = psutil.virtual_memory()
            print(f"📊 System: {mem.available / 1024**3:.1f}GB RAM available, {cores} core(s) configured", file=sys.stderr)
        except:
            pass
        
        print(f"🎲 Running {self.config.num_tests} random tests (cores: {cores})...", file=sys.stderr)
        
        # Always use sequential - parallel is handled at batch_runner level
        for i in range(self.config.num_tests):
            params = self.param_space.sample_random()
            result = self._run_single_test_safe(params, test_func, i+1)
            
            if result:
                self.results.append(result)
                
                if result['score'] > self.best_score:
                    self.best_score = result['score']
                    self.best_params = result['params']
                    print(f"  🎯 New best! Score: {self.best_score:.3f}, Return: {result.get('totalReturn', 0):.1f}%", file=sys.stderr)
        
        print(f"✅ Completed: {len(self.results)} valid results out of {self.config.num_tests} tests", file=sys.stderr)
        return self.results
    
    def optimize_grid(self, test_func: Callable) -> List[Dict]:
        """Systematic grid search"""
        print(f"📊 Running grid search...")
        
        count = 0
        for params in self.param_space.grid_iterator():
            if count >= self.config.num_tests:
                break
            
            result = test_func(params)
            
            if self.should_abort(result):
                print(f"  ⚠️  Test {count+1} aborted (stop condition met)")
                count += 1
                continue
            
            score = self.calculate_score(result)
            result['score'] = score
            result['params'] = params
            self.results.append(result)
            
            if score > self.best_score:
                self.best_score = score
                self.best_params = params
                print(f"  🎯 New best! Score: {score:.3f}, Return: {result.get('totalReturn', 0):.1f}%")
            
            count += 1
        
        return self.results
    
    def optimize_annealing(self, test_func: Callable) -> List[Dict]:
        """Simulated annealing - gets 'warmer' when finding better results"""
        print(f"🔥 Running simulated annealing...")
        
        # Start with random params
        current_params = self.param_space.sample_random()
        current_result = test_func(current_params)
        current_score = self.calculate_score(current_result)
        
        best_params = current_params
        best_score = current_score
        best_result = current_result
        
        temperature = self.config.annealing_temp_start
        
        for i in range(self.config.num_tests):
            # Generate neighbor (slightly modified params)
            neighbor_params = self._generate_neighbor(current_params)
            neighbor_result = test_func(neighbor_params)
            
            if self.should_abort(neighbor_result):
                continue
            
            neighbor_score = self.calculate_score(neighbor_result)
            
            # Accept if better, or with probability based on temperature
            delta = neighbor_score - current_score
            if delta > 0 or random.random() < np.exp(delta / temperature):
                current_params = neighbor_params
                current_score = neighbor_score
                current_result = neighbor_result
                
                if current_score > best_score:
                    best_score = current_score
                    best_params = current_params
                    best_result = current_result
                    print(f"  🔥 Warmer! Score: {best_score:.3f}, Return: {best_result.get('totalReturn', 0):.1f}%, Temp: {temperature:.3f}")
            
            # Cool down
            temperature *= self.config.annealing_cooling_rate
            
            neighbor_result['score'] = neighbor_score
            neighbor_result['params'] = neighbor_params
            self.results.append(neighbor_result)
        
        self.best_score = best_score
        self.best_params = best_params
        
        return self.results
    
    def _generate_neighbor(self, params: Dict) -> Dict:
        """Generate a neighboring parameter set (for annealing)"""
        neighbor = params.copy()
        
        # Randomly modify 1-2 parameters
        num_to_modify = random.randint(1, min(2, len(params)))
        params_to_modify = random.sample(list(params.keys()), num_to_modify)
        
        for param_name in params_to_modify:
            spec = self.param_space.param_ranges[param_name]
            
            if isinstance(spec, list):
                # Pick a different value from the list
                neighbor[param_name] = random.choice(spec)
            elif isinstance(spec, dict):
                # Slightly modify the value
                current_val = params[param_name]
                step = spec.get('step', 0.1)
                
                # Move up or down by 1-3 steps
                delta = random.randint(-3, 3) * step
                new_val = current_val + delta
                
                # Clamp to range
                new_val = max(spec['min'], min(spec['max'], new_val))
                
                if isinstance(spec['min'], int):
                    neighbor[param_name] = int(new_val)
                else:
                    neighbor[param_name] = round(new_val, 2)
        
        return neighbor
    
    def run(self, test_func: Callable) -> List[Dict]:
        """Run optimization with selected strategy"""
        if self.config.strategy == 'random':
            return self.optimize_random(test_func)
        elif self.config.strategy == 'grid':
            return self.optimize_grid(test_func)
        elif self.config.strategy == 'annealing':
            return self.optimize_annealing(test_func)
        elif self.config.strategy == 'genetic':
            # TODO: Implement genetic algorithm
            print("⚠️  Genetic algorithm not yet implemented, falling back to random")
            return self.optimize_random(test_func)
        elif self.config.strategy == 'bayesian':
            # TODO: Implement Bayesian optimization
            print("⚠️  Bayesian optimization not yet implemented, falling back to random")
            return self.optimize_random(test_func)
        else:
            raise ValueError(f"Unknown strategy: {self.config.strategy}")
    
    def get_top_results(self, n: int = 10, sort_by: str = 'score') -> List[Dict]:
        """Get top N results sorted by specified metric"""
        sorted_results = sorted(
            self.results,
            key=lambda x: x.get(sort_by, 0),
            reverse=True
        )
        return sorted_results[:n]


if __name__ == '__main__':
    # Example usage
    param_space = ParameterSpace({
        'leverage': [2, 3, 4, 5, 10],
        'stop_loss': {'min': 2.0, 'max': 10.0, 'step': 0.5},
        'rsi_period': {'min': 7, 'max': 21, 'step': 1},
    })
    
    config = OptimizationConfig(
        strategy='annealing',
        num_tests=50,
        max_drawdown_threshold=50.0,
        min_win_rate_threshold=40.0,
    )
    
    optimizer = BatchOptimizer(config, param_space)
    
    # Mock test function
    def mock_test(params):
        return {
            'totalReturn': random.uniform(-50, 200),
            'sharpeRatio': random.uniform(0, 4),
            'winRate': random.uniform(30, 70),
            'maxDrawdown': random.uniform(-80, -10),
        }
    
    results = optimizer.run(mock_test)
    top_10 = optimizer.get_top_results(10)
    
    print(f"\n✅ Optimization complete!")
    print(f"Total tests: {len(results)}")
    print(f"Best score: {optimizer.best_score:.3f}")
    print(f"Best params: {optimizer.best_params}")

