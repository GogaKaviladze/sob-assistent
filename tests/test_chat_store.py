"""Unit tests for SessionStore interface and TTL-based ChatSessionStore."""
import time
from unittest.mock import patch

import pytest

from sob_generator.chat_session import ChatSession
from sob_generator.chat_store import ChatSessionStore, SessionStore
from sob_generator.models import ClientData, ReportMeta


def _create_dummy_session(session_id: str = "sess-1") -> ChatSession:
    return ChatSession(
        session_id=session_id,
        client=ClientData(name="Mustermann", vorname="Max"),
        meta=ReportMeta(),
    )


def test_session_store_interface_compliance():
    store = ChatSessionStore()
    assert isinstance(store, SessionStore)


def test_store_and_get_session():
    store = ChatSessionStore(ttl_seconds=3600.0)
    sess = _create_dummy_session("s1")
    store.store(sess)

    retrieved = store.get("s1")
    assert retrieved is not None
    assert retrieved.session_id == "s1"
    assert retrieved.client.name == "Mustermann"


def test_get_nonexistent_session_returns_none():
    store = ChatSessionStore()
    assert store.get("nonexistent") is None


def test_discard_removes_session_immediately():
    store = ChatSessionStore()
    sess = _create_dummy_session("s1")
    store.store(sess)
    assert store.get("s1") is not None

    store.discard("s1")
    assert store.get("s1") is None


def test_clear_removes_all_sessions():
    store = ChatSessionStore()
    store.store(_create_dummy_session("s1"))
    store.store(_create_dummy_session("s2"))

    store.clear()
    assert store.get("s1") is None
    assert store.get("s2") is None


def test_ttl_expiration_on_get():
    store = ChatSessionStore(ttl_seconds=10.0)
    sess = _create_dummy_session("s1")

    now = 1000.0
    with patch("time.time", return_value=now):
        store.store(sess)

    # Within TTL
    with patch("time.time", return_value=now + 5.0):
        assert store.get("s1") is not None

    # After TTL (idle > 10.0s since last access at now + 5.0s)
    with patch("time.time", return_value=now + 16.0):
        assert store.get("s1") is None


def test_cleanup_expired_purges_only_expired_sessions():
    store = ChatSessionStore(ttl_seconds=10.0)
    s1 = _create_dummy_session("s1")
    s2 = _create_dummy_session("s2")

    now = 1000.0
    with patch("time.time", return_value=now):
        store.store(s1)

    with patch("time.time", return_value=now + 5.0):
        store.store(s2)

    # At now + 12.0: s1 is expired (>10s old), s2 is still fresh (7s old)
    with patch("time.time", return_value=now + 12.0):
        purged = store.cleanup_expired()
        assert purged == 1
        assert store.get("s1") is None
        assert store.get("s2") is not None
