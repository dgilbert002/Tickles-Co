# Code Rules

## File Template
Every new Python file must follow this structure:
```python
"""
Module: [name]
Purpose: [one line description]
Location: /opt/tickles/[path]
"""

import logging
from typing import ...

logger = logging.getLogger(__name__)

# Constants
...

# Classes/Functions
...
```

## Implementation Checklist
Before marking any implementation complete:
- [ ] All functions have docstrings
- [ ] All functions have type hints
- [ ] All functions have error handling with specific exceptions
- [ ] No hardcoded values (use config or env vars)
- [ ] No print() statements (use logging)
- [ ] No TODO/FIXME comments
- [ ] __init__.py exists in every package directory
- [ ] A basic test file exists (test_[module].py)
- [ ] The code actually runs without errors

## Database Code Patterns
```python
# CORRECT — parameterized query
cursor.execute("SELECT * FROM trades WHERE strategy_id = %s", (strategy_id,))

# WRONG — string interpolation (SQL injection risk)
cursor.execute(f"SELECT * FROM trades WHERE strategy_id = {strategy_id}")
```

Always use connection pooling for services:
```python
from mysql.connector import pooling
pool = pooling.MySQLConnectionPool(pool_name="tickles", pool_size=5, **db_config)
```

## Config Pattern
```python
import os

class Config:
    DB_HOST = os.environ.get("DB_HOST", "localhost")
    DB_PORT = int(os.environ.get("DB_PORT", "3306"))
    DB_USER = os.environ.get("DB_USER", "admin")
    DB_PASS = os.environ.get("DB_PASS", "")
```

## Retry Pattern
```python
import time

def retry(func, max_attempts=3, backoff=2):
    for attempt in range(max_attempts):
        try:
            return func()
        except Exception as e:
            if attempt == max_attempts - 1:
                raise
            wait = backoff ** attempt
            logger.warning(f"Attempt {attempt+1} failed: {e}. Retrying in {wait}s...")
            time.sleep(wait)
```

## What To Do When Stuck
1. Re-read the Architect's design document for this component.
2. Check /opt/tickles/CLAUDE.md for how existing components are connected.
3. Check if a similar pattern already exists in the codebase — reuse, don't reinvent.
4. If genuinely stuck, signal completion with a clear description of the blocker.
