"""Session Store for interactive chat sessions (GDPR Art. 25 compliant).

Stores ongoing interactive chat sessions in RAM with TTL-based idle expiration.
Provides an abstract SessionStore interface for pluggable persistence backends.
"""
from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod
from typing import Dict, Optional, Tuple

from sob_generator.chat_session import ChatSession

DEFAULT_TTL_SECONDS = 7200.0  # 2 hours idle timeout


class SessionStore(ABC):
    """Abstract interface for session storage."""

    @abstractmethod
    def store(self, session: ChatSession) -> ChatSession:
        """Saves or updates a chat session."""
        ...

    @abstractmethod
    def get(self, session_id: str) -> Optional[ChatSession]:
        """Retrieves a chat session by id, or None if expired/not found."""
        ...

    @abstractmethod
    def discard(self, session_id: str) -> None:
        """Discards a session immediately."""
        ...

    @abstractmethod
    def clear(self) -> None:
        """Clears all sessions from the store."""
        ...

    @abstractmethod
    def cleanup_expired(self) -> int:
        """Removes all expired sessions and returns count of removed sessions."""
        ...


class ChatSessionStore(SessionStore):
    """Thread-safe in-memory store for ChatSession instances with TTL expiration."""

    def __init__(self, ttl_seconds: float = DEFAULT_TTL_SECONDS) -> None:
        self.ttl_seconds = ttl_seconds
        self._sessions: Dict[str, Tuple[ChatSession, float]] = {}
        self._lock = threading.Lock()

    def store(self, session: ChatSession) -> ChatSession:
        """Saves or updates a chat session and refreshes its last-accessed timestamp."""
        now = time.time()
        with self._lock:
            self._sessions[session.session_id] = (session, now)
        return session

    def get(self, session_id: str) -> Optional[ChatSession]:
        """Retrieves a session if it exists and has not expired.
        
        Refreshes the last-accessed timestamp on active retrieval.
        """
        now = time.time()
        with self._lock:
            entry = self._sessions.get(session_id)
            if entry is None:
                return None
            session, last_accessed = entry
            if self.ttl_seconds > 0 and (now - last_accessed) > self.ttl_seconds:
                # Expired -> remove immediately
                del self._sessions[session_id]
                return None
            # Update last accessed
            self._sessions[session_id] = (session, now)
            return session

    def discard(self, session_id: str) -> None:
        """Discards session immediately from RAM (GDPR cleanup)."""
        with self._lock:
            self._sessions.pop(session_id, None)

    def clear(self) -> None:
        """Clears all sessions from memory."""
        with self._lock:
            self._sessions.clear()

    def cleanup_expired(self) -> int:
        """Iterates through all sessions and purges those whose idle TTL has expired."""
        now = time.time()
        removed = 0
        with self._lock:
            expired_ids = [
                sid
                for sid, (_, last_accessed) in self._sessions.items()
                if self.ttl_seconds > 0 and (now - last_accessed) > self.ttl_seconds
            ]
            for sid in expired_ids:
                del self._sessions[sid]
                removed += 1
        return removed
