"""
Centralized Database Configuration for Python Scripts

This module provides a single source of truth for database connections.
It handles:
1. Reading DATABASE_URL from environment variable (set by TypeScript when spawning)
2. Fallback to reading from config/database.json for standalone script execution
3. Proper URL parsing with support for special characters in passwords

Usage:
    from db_config import get_database_url, get_db_connection

    # Get connection string
    db_url = get_database_url()
    
    # Or get a ready-to-use connection
    conn = get_db_connection()
"""

import os
import json
import mysql.connector
from urllib.parse import urlparse, unquote
from typing import Optional, Dict, Any
from pathlib import Path


def get_project_root() -> Path:
    """Get the project root directory (parent of python_engine)"""
    return Path(__file__).parent.parent


def get_database_url() -> str:
    """
    Get DATABASE_URL from environment or config file.
    
    Priority:
    1. DATABASE_URL environment variable (set by TypeScript when spawning Python)
    2. config/database.json file (for standalone script execution)
    
    Returns:
        Database URL string
        
    Raises:
        ValueError if no database URL found
    """
    # First, check environment variable (TypeScript passes this when spawning)
    db_url = os.getenv('DATABASE_URL')
    if db_url:
        return db_url
    
    # Fallback: read from config file
    config_paths = [
        get_project_root() / 'config' / 'database.json',
        get_project_root() / 'database.json',
        Path.home() / '.capitaltwo' / 'database.json',
    ]
    
    for config_path in config_paths:
        if config_path.exists():
            try:
                with open(config_path, 'r') as f:
                    config = json.load(f)
                    if 'DATABASE_URL' in config:
                        return config['DATABASE_URL']
            except (json.JSONDecodeError, IOError) as e:
                print(f"Warning: Could not read {config_path}: {e}")
                continue
    
    # If we get here, no database URL found
    raise ValueError(
        "DATABASE_URL not found!\n"
        "Options to fix:\n"
        "1. Set DATABASE_URL environment variable\n"
        "2. Create config/database.json with: {\"DATABASE_URL\": \"mysql://user:pass@host:port/database\"}\n"
        "3. If running from TypeScript, ensure process.env is passed to spawn()"
    )


def parse_database_url(url: str) -> Dict[str, Any]:
    """
    Parse DATABASE_URL into connection parameters.
    
    Handles URL-encoded special characters in passwords (like % @ : etc.)
    
    Args:
        url: MySQL connection URL (mysql://user:pass@host:port/database)
        
    Returns:
        Dictionary with host, port, user, password, database keys
    """
    parsed = urlparse(url)
    return {
        "host": parsed.hostname,
        "port": parsed.port or 3306,
        "user": unquote(parsed.username) if parsed.username else None,
        "password": unquote(parsed.password) if parsed.password else None,
        "database": parsed.path.lstrip("/").split("?")[0],  # Remove query params too
    }


def get_db_connection():
    """
    Get a MySQL database connection.
    
    Returns:
        mysql.connector connection object
        
    Raises:
        ValueError if DATABASE_URL not found
        mysql.connector.Error if connection fails
    """
    db_url = get_database_url()
    config = parse_database_url(db_url)
    return mysql.connector.connect(**config)


def test_connection() -> bool:
    """
    Test database connection.
    
    Returns:
        True if connection successful, False otherwise
    """
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT 1")
        cursor.fetchone()
        cursor.close()
        conn.close()
        return True
    except Exception as e:
        print(f"Database connection test failed: {e}")
        return False


# For convenience, also expose these at module level
def get_connection_info() -> Dict[str, Any]:
    """Get parsed connection info (without actually connecting)"""
    return parse_database_url(get_database_url())


if __name__ == '__main__':
    """Test the database configuration when run directly"""
    print("Testing database configuration...")
    print("-" * 50)
    
    try:
        db_url = get_database_url()
        print(f"✓ DATABASE_URL found")
        
        # Show parsed info (hide password)
        info = parse_database_url(db_url)
        print(f"  Host: {info['host']}")
        print(f"  Port: {info['port']}")
        print(f"  User: {info['user']}")
        print(f"  Password: {'*' * len(info['password']) if info['password'] else 'None'}")
        print(f"  Database: {info['database']}")
        
        print("\nTesting connection...")
        if test_connection():
            print("✓ Database connection successful!")
        else:
            print("✗ Database connection failed")
            
    except ValueError as e:
        print(f"✗ Configuration error: {e}")
    except Exception as e:
        print(f"✗ Unexpected error: {e}")

