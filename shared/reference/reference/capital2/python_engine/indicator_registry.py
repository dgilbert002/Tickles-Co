"""
Indicator Registry - Metadata and organization for all 162+ indicators
Provides categorization, direction tagging, and parameter information
"""

import json
from typing import Dict, List, Any
from indicators_comprehensive import INDICATOR_METADATA, create_indicator_library

class IndicatorRegistry:
    """
    Central registry for all indicators with metadata
    """
    
    def __init__(self):
        self.metadata = INDICATOR_METADATA
        self.factory = create_indicator_library()
    
    def get_all_indicators(self) -> List[str]:
        """Get list of all indicator names"""
        return sorted(self.metadata.keys())
    
    def get_by_direction(self, direction: str) -> List[str]:
        """Get indicators filtered by direction (bullish/bearish/neutral)"""
        return sorted([
            name for name, meta in self.metadata.items()
            if meta['direction'] == direction
        ])
    
    def get_by_category(self, category: str) -> List[str]:
        """Get indicators filtered by category"""
        return sorted([
            name for name, meta in self.metadata.items()
            if meta['category'] == category
        ])
    
    def get_metadata(self, indicator_name: str) -> Dict[str, Any]:
        """Get metadata for a specific indicator"""
        return self.metadata.get(indicator_name, {})
    
    def get_default_params(self, indicator_name: str) -> Dict[str, Any]:
        """Get default parameters for an indicator"""
        meta = self.metadata.get(indicator_name, {})
        return meta.get('params', {})
    
    def get_param_ranges(self, indicator_name: str) -> Dict[str, List]:
        """Get parameter ranges for optimization"""
        meta = self.metadata.get(indicator_name, {})
        return meta.get('param_ranges', {})
    
    def get_organized_list(self) -> Dict[str, Any]:
        """
        Get indicators organized by direction and category
        Returns a nested structure for UI display
        """
        result = {
            'by_direction': {
                'bullish': [],
                'bearish': [],
                'neutral': []
            },
            'by_category': {
                'trend': [],
                'momentum': [],
                'volatility': [],
                'volume': [],
                'breakout': [],
                'crash_protection': [],
                'combination': [],
                'smart_money': []
            },
            'total': len(self.metadata)
        }
        
        for name, meta in self.metadata.items():
            direction = meta['direction']
            category = meta['category']
            
            indicator_info = {
                'name': name,
                'display_name': name.replace('_', ' ').title(),
                'description': meta.get('description', ''),
                'direction': direction,
                'category': category,
                'params': meta.get('params', {}),
                'param_ranges': meta.get('param_ranges', {})
            }
            
            # Handle unknown directions gracefully
            if direction not in result['by_direction']:
                result['by_direction'][direction] = []
            result['by_direction'][direction].append(indicator_info)
            
            # Handle unknown categories gracefully
            if category not in result['by_category']:
                result['by_category'][category] = []
            result['by_category'][category].append(indicator_info)
        
        # Sort each list by name
        for direction in result['by_direction']:
            result['by_direction'][direction].sort(key=lambda x: x['name'])
        for category in result['by_category']:
            result['by_category'][category].sort(key=lambda x: x['name'])
        
        return result
    
    def get_summary(self) -> Dict[str, Any]:
        """Get summary statistics"""
        by_direction = {}
        by_category = {}
        
        for name, meta in self.metadata.items():
            direction = meta['direction']
            category = meta['category']
            
            by_direction[direction] = by_direction.get(direction, 0) + 1
            by_category[category] = by_category.get(category, 0) + 1
        
        return {
            'total': len(self.metadata),
            'by_direction': by_direction,
            'by_category': by_category
        }


# Singleton instance
_registry = None

def get_indicator_registry() -> IndicatorRegistry:
    """Get the singleton indicator registry instance"""
    global _registry
    if _registry is None:
        _registry = IndicatorRegistry()
    return _registry


if __name__ == '__main__':
    # Test the registry
    registry = get_indicator_registry()
    
    print("=" * 80)
    print("INDICATOR REGISTRY TEST")
    print("=" * 80)
    
    summary = registry.get_summary()
    print(f"\nTotal Indicators: {summary['total']}")
    print(f"\nBy Direction:")
    for direction, count in summary['by_direction'].items():
        print(f"  {direction.capitalize()}: {count}")
    print(f"\nBy Category:")
    for category, count in summary['by_category'].items():
        print(f"  {category.replace('_', ' ').title()}: {count}")
    
    print(f"\nBullish Indicators (first 10):")
    bullish = registry.get_by_direction('bullish')
    for i, name in enumerate(bullish[:10], 1):
        meta = registry.get_metadata(name)
        print(f"  {i}. {name} - {meta.get('description', '')}")
    
    print(f"\nMomentum Indicators (first 10):")
    momentum = registry.get_by_category('momentum')
    for i, name in enumerate(momentum[:10], 1):
        meta = registry.get_metadata(name)
        print(f"  {i}. {name} - {meta.get('description', '')}")
    
    print("\n" + "=" * 80)

