"""
Module: sessions
Purpose: DST-aware trading session registry and validator.
Location: /opt/tickles/shared/utils/sessions.py
"""

import logging
from datetime import datetime, time, timedelta, timezone
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from .db import DatabasePool

logger = logging.getLogger(__name__)

class SessionDefinition:
    """Day-specific open/close times for a session."""
    def __init__(self, day_of_week: int, open_time: time, close_time: time):
        self.day_of_week = day_of_week
        self.open_time = open_time
        self.close_time = close_time

class TradingSession:
    """A named trading session with timezone and weekly schedule."""
    def __init__(self, name: str, tz_name: str, definitions: List[SessionDefinition]):
        self.name = name
        self.tz = ZoneInfo(tz_name)
        self.schedule: Dict[int, SessionDefinition] = {d.day_of_week: d for d in definitions}

    def is_open(self, dt: datetime) -> bool:
        """Check if the session is open at the given UTC datetime."""
        # Convert UTC to session local time
        local_dt = dt.astimezone(self.tz)
        day = local_dt.weekday()
        
        if day not in self.schedule:
            return False
            
        defn = self.schedule[day]
        local_time = local_dt.time()
        
        if defn.open_time <= defn.close_time:
            return defn.open_time <= local_time <= defn.close_time
        else:
            # Overnight session (e.g. 22:00 to 04:00)
            return local_time >= defn.open_time or local_time <= defn.close_time

    def get_next_close(self, dt: datetime) -> Optional[datetime]:
        """Get the next scheduled close time after the given UTC datetime."""
        local_dt = dt.astimezone(self.tz)
        
        # Check today and next 7 days
        for i in range(8):
            check_date = (local_dt + timedelta(days=i)).date()
            day = check_date.weekday()
            
            if day in self.schedule:
                defn = self.schedule[day]
                close_dt = datetime.combine(check_date, defn.close_time, tzinfo=self.tz)
                
                if close_dt > local_dt:
                    return close_dt.astimezone(timezone.utc)
        
        return None

class SessionService:
    """Registry for all trading sessions, backed by Postgres."""
    
    def __init__(self, pool: DatabasePool):
        self._pool = pool
        self._cache: Dict[str, TradingSession] = {}

    async def load_sessions(self) -> None:
        """Eagerly load all sessions from the database."""
        try:
            sessions_rows = await self._pool.fetch_all(
                "SELECT id, name, timezone FROM public.sessions"
            )
            
            for s_row in sessions_rows:
                defn_rows = await self._pool.fetch_all(
                    "SELECT day_of_week, open_time, close_time FROM public.session_definitions WHERE session_id = $1 AND is_active = TRUE",
                    (s_row["id"],)
                )
                
                defns = [
                    SessionDefinition(r["day_of_week"], r["open_time"], r["close_time"])
                    for r in defn_rows
                ]
                
                self._cache[s_row["name"]] = TradingSession(s_row["name"], s_row["timezone"], defns)
                
            logger.info("Loaded %d trading sessions from database", len(self._cache))
        except Exception as e:
            logger.error("Failed to load trading sessions: %s", e)
            raise

    async def get_session(self, name: str) -> TradingSession:
        """Get a session by name, loading it if not in cache."""
        if name not in self._cache:
            # Try to load just this one
            row = await self._pool.fetch_one(
                "SELECT id, name, timezone FROM public.sessions WHERE name = $1",
                (name,)
            )
            if not row:
                raise ValueError(f"Session '{name}' not found in database")
                
            defn_rows = await self._pool.fetch_all(
                "SELECT day_of_week, open_time, close_time FROM public.session_definitions WHERE session_id = $1 AND is_active = TRUE",
                (row["id"],)
            )
            
            defns = [
                SessionDefinition(r["day_of_week"], r["open_time"], r["close_time"])
                for r in defn_rows
            ]
            
            self._cache[name] = TradingSession(name, row["timezone"], defns)
            
        return self._cache[name]

    async def is_open(self, session_name: str, dt: Optional[datetime] = None) -> bool:
        """Check if a session is open at a specific UTC time (default now)."""
        if dt is None:
            dt = datetime.now(timezone.utc)
        
        session = await self.get_session(session_name)
        return session.is_open(dt)
