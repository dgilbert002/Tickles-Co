---
name: documentation
description: Generate or update project documentation. Use when asked to document code, update CLAUDE.md, write README files, or explain system components.
---

# Documentation Skill

## When Activated
User asks to "document", "update docs", "write README", "explain this component", or "update CLAUDE.md".

## Documentation Types

### CLAUDE.md Updates
When infrastructure changes, update /opt/tickles/CLAUDE.md:
- Add new services to the services table
- Add new databases/tables to the database section
- Add new scripts or tools
- Update folder structure if changed
- Keep it concise — CLAUDE.md is a quick reference, not a novel

### Component README
For each new service or module, create a README.md in its directory:
```markdown
# [Component Name]

## Purpose
[One paragraph explaining what this does and why it exists]

## Usage
[How to use this component — function calls, CLI commands, or API endpoints]

## Configuration
[What environment variables or config files it needs]

## Dependencies
[What it connects to — databases, APIs, other services]

## Example
[A complete working example]
```

### Code Documentation
- Every Python file has a module docstring at the top
- Every class has a class docstring
- Every public function has a Google-style docstring:
```python
def fetch_candles(exchange: str, symbol: str, timeframe: str) -> list[dict]:
    """Fetch OHLCV candle data from an exchange.

    Args:
        exchange: Exchange name (e.g., 'binance', 'bybit')
        symbol: Trading pair (e.g., 'BTC/USDT')
        timeframe: Candle timeframe (e.g., '1h', '5m')

    Returns:
        List of candle dicts with keys: open, high, low, close, volume, timestamp

    Raises:
        ConnectionError: If exchange API is unreachable
        ValueError: If symbol is not valid on the exchange
    """
```

## After Documenting
- Verify the documentation matches the actual code
- Check that all file paths referenced actually exist
- Update MemClaw workspace with the documentation change
