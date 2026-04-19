"""Phase 36 — Owner Dashboard + Telegram OTP + Mobile tests."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from io import StringIO
from unittest.mock import patch

import pytest
from aiohttp.test_utils import TestClient, TestServer  # noqa: F401

from shared.dashboard import (
    AuthConfig,
    DashboardAuth,
    DashboardUser,
    DisabledUser,
    InMemoryDashboardPool,
    InvalidOtp,
    InvalidSession,
    MIGRATION_PATH,
    NullTelegramSender,
    RegistryServicesProvider,
    SnapshotBuilder,
    SnapshotProviders,
    SubmissionsStoreProvider,
    UnknownChat,
    build_auth_from_pool,
    hash_secret,
    read_migration_sql,
    snapshot_to_dict,
)
from shared.dashboard.server import build_app
from shared.dashboard.store import (
    DashboardSessionStore,
    DashboardUserStore,
    now_utc,
)
from shared.backtest_submit import BacktestSpec, BacktestSubmissionStore
from shared.backtest_submit.memory_pool import InMemoryBacktestSubmitPool
from shared.cli import dashboard_cli


# -------------------------------------------------------------- migration


def test_migration_exists_and_has_expected_tables():
    assert MIGRATION_PATH.exists()
    sql = read_migration_sql()
    for t in ("dashboard_users", "dashboard_otps", "dashboard_sessions",
              "dashboard_sessions_active"):
        assert t in sql, t


# --------------------------------------------------------------- hashing


def test_hash_secret_is_stable_and_distinct():
    assert hash_secret("123456") == hash_secret("123456")
    assert hash_secret("abcdef") != hash_secret("abcdef ")
    assert len(hash_secret("x")) == 64


# ------------------------------------------------------------- user store


def test_user_store_upsert_and_list():
    async def _run():
        pool = InMemoryDashboardPool()
        users = DashboardUserStore(pool)
        u1 = await users.upsert(DashboardUser(
            id=None, chat_id="1001", display_name="owner",
        ))
        u2 = await users.upsert(DashboardUser(
            id=None, chat_id="1002", display_name="viewer",
            role="viewer",
        ))
        await users.upsert(DashboardUser(
            id=None, chat_id="1001", display_name="owner renamed",
            role="owner", enabled=True,
        ))
        assert u1 != u2
        by_id = {u.chat_id: u for u in await users.list()}
        assert by_id["1001"].display_name == "owner renamed"
        assert by_id["1002"].role == "viewer"
    asyncio.run(_run())


def test_user_store_set_enabled():
    async def _run():
        pool = InMemoryDashboardPool()
        users = DashboardUserStore(pool)
        await users.upsert(DashboardUser(id=None, chat_id="1"))
        await users.set_enabled("1", False)
        u = await users.get("1")
        assert u.enabled is False
    asyncio.run(_run())


# -------------------------------------------------------------- OTP flow


def _fresh_auth(now_fn=now_utc, config=None):
    pool = InMemoryDashboardPool()
    auth = build_auth_from_pool(
        pool, sender=NullTelegramSender(path="/dev/null"),
        config=config or AuthConfig(),
        now_fn=now_fn,
    )
    return pool, auth


def test_issue_otp_rejects_unknown_chat():
    async def _run():
        _, auth = _fresh_auth()
        with pytest.raises(UnknownChat):
            await auth.issue_otp("nobody")
    asyncio.run(_run())


def test_issue_otp_rejects_disabled_user():
    async def _run():
        pool, auth = _fresh_auth()
        users = DashboardUserStore(pool)
        await users.upsert(DashboardUser(
            id=None, chat_id="1", enabled=False,
        ))
        with pytest.raises(DisabledUser):
            await auth.issue_otp("1")
    asyncio.run(_run())


def test_issue_and_verify_otp_happy_path():
    async def _run():
        pool, auth = _fresh_auth()
        users = DashboardUserStore(pool)
        await users.upsert(DashboardUser(id=None, chat_id="77"))
        issued = await auth.issue_otp("77")
        assert len(issued.code) == 6 and issued.code.isdigit()
        assert issued.delivery_ok is True
        session = await auth.verify_otp("77", issued.code,
                                        user_agent="tests")
        assert session.token and session.session_id
        user, sess = await auth.authenticate_token(session.token)
        assert user.chat_id == "77"
        assert sess.chat_id == "77"
    asyncio.run(_run())


def test_verify_otp_rejects_wrong_code():
    async def _run():
        pool, auth = _fresh_auth()
        await DashboardUserStore(pool).upsert(
            DashboardUser(id=None, chat_id="5"),
        )
        await auth.issue_otp("5")
        with pytest.raises(InvalidOtp):
            await auth.verify_otp("5", "000000")
    asyncio.run(_run())


def test_verify_otp_rejects_consumed_code():
    async def _run():
        pool, auth = _fresh_auth()
        await DashboardUserStore(pool).upsert(
            DashboardUser(id=None, chat_id="5"),
        )
        issued = await auth.issue_otp("5")
        await auth.verify_otp("5", issued.code)
        with pytest.raises(InvalidOtp):
            await auth.verify_otp("5", issued.code)
    asyncio.run(_run())


def test_verify_otp_rejects_expired_code():
    async def _run():
        now = datetime.now(timezone.utc)

        def clock(offset=[0]):
            return now + timedelta(seconds=offset[0])

        pool = InMemoryDashboardPool()
        auth = DashboardAuth(
            users=DashboardUserStore(pool),
            otps=__import__(
                "shared.dashboard.store", fromlist=["DashboardOtpStore"],
            ).DashboardOtpStore(pool),
            sessions=DashboardSessionStore(pool),
            sender=NullTelegramSender(path="/dev/null"),
            config=AuthConfig(otp_ttl_s=60),
        )
        await DashboardUserStore(pool).upsert(
            DashboardUser(id=None, chat_id="5"),
        )
        issued = await auth.issue_otp("5")
        # Force every otp to be "expired" in the in-memory pool.
        for o in pool.otps:
            o["expires_at"] = now - timedelta(seconds=1)
        with pytest.raises(InvalidOtp):
            await auth.verify_otp("5", issued.code)
    asyncio.run(_run())


# ------------------------------------------------------------- sessions


def test_authenticate_rejects_unknown_token():
    async def _run():
        _, auth = _fresh_auth()
        with pytest.raises(InvalidSession):
            await auth.authenticate_token("nope")
    asyncio.run(_run())


def test_authenticate_rejects_revoked_session():
    async def _run():
        pool, auth = _fresh_auth()
        await DashboardUserStore(pool).upsert(
            DashboardUser(id=None, chat_id="9"),
        )
        issued = await auth.issue_otp("9")
        session = await auth.verify_otp("9", issued.code)
        await auth.revoke(session.session_id)
        with pytest.raises(InvalidSession):
            await auth.authenticate_token(session.token)
    asyncio.run(_run())


def test_revoke_all_for_chat_id():
    async def _run():
        pool, auth = _fresh_auth()
        await DashboardUserStore(pool).upsert(
            DashboardUser(id=None, chat_id="10"),
        )
        issued1 = await auth.issue_otp("10")
        await auth.verify_otp("10", issued1.code)
        issued2 = await auth.issue_otp("10")
        await auth.verify_otp("10", issued2.code)
        assert (
            len(await DashboardSessionStore(pool).list_active(chat_id="10"))
            == 2
        )
        n = await auth.revoke_all_for("10")
        assert n == 2
        assert (
            len(await DashboardSessionStore(pool).list_active(chat_id="10"))
            == 0
        )
    asyncio.run(_run())


# ---------------------------------------------------------------- snapshot


def test_snapshot_builder_returns_dict_with_notes():
    async def _run():
        providers = SnapshotProviders()
        providers.services = RegistryServicesProvider()

        bt_pool = InMemoryBacktestSubmitPool()
        bt_store = BacktestSubmissionStore(bt_pool)
        providers.submissions = SubmissionsStoreProvider(bt_store)

        from shared.backtest_submit import BacktestSubmitter
        from shared.backtest_submit.submitter import InMemoryQueue
        submitter = BacktestSubmitter(bt_store, InMemoryQueue())
        for strat, sym in (("rsi", "BTC/USDT"), ("ma", "ETH/USDT")):
            await submitter.submit(BacktestSpec(strategy=strat, symbols=[sym]))

        builder = SnapshotBuilder(providers=providers)
        snap = await builder.build()
        data = snapshot_to_dict(snap)
        assert data["services_total"] > 0
        assert data["submissions_active"] == 2
        assert "intents: provider not wired" in data["notes"]
    asyncio.run(_run())


# --------------------------------------------------------------- HTTP API


async def _mk_client_with_user():
    pool = InMemoryDashboardPool()
    await DashboardUserStore(pool).upsert(
        DashboardUser(id=None, chat_id="42"),
    )
    auth = build_auth_from_pool(
        pool, sender=NullTelegramSender(path="/dev/null"),
    )
    providers = SnapshotProviders()
    providers.services = RegistryServicesProvider()
    app = build_app(auth, providers, expose_otp=True)
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    return client, auth


def test_http_otp_flow_and_snapshot():
    async def _run():
        client, _ = await _mk_client_with_user()
        try:
            r = await client.post("/api/auth/request-otp",
                                  json={"chat_id": "42"})
            assert r.status == 200
            data = await r.json()
            code = data["code"]

            r2 = await client.post("/api/auth/verify-otp",
                                   json={"chat_id": "42", "code": code})
            assert r2.status == 200
            payload = await r2.json()
            token = payload["token"]

            r3 = await client.get(
                "/api/snapshot",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert r3.status == 200
            snap = await r3.json()
            assert snap["services_total"] > 0

            r4 = await client.get("/api/snapshot")
            assert r4.status == 401
        finally:
            await client.close()
    asyncio.run(_run())


def test_http_request_otp_unknown_chat_returns_404():
    async def _run():
        client, _ = await _mk_client_with_user()
        try:
            r = await client.post("/api/auth/request-otp",
                                  json={"chat_id": "nobody"})
            assert r.status == 404
        finally:
            await client.close()
    asyncio.run(_run())


def test_http_logout_revokes_session():
    async def _run():
        client, _ = await _mk_client_with_user()
        try:
            r1 = await client.post(
                "/api/auth/request-otp", json={"chat_id": "42"},
            )
            code = (await r1.json())["code"]
            r2 = await client.post(
                "/api/auth/verify-otp",
                json={"chat_id": "42", "code": code},
            )
            token = (await r2.json())["token"]
            r = await client.post(
                "/api/auth/logout",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert r.status == 200
            r3 = await client.get(
                "/api/snapshot",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert r3.status == 401
        finally:
            await client.close()
    asyncio.run(_run())


def test_http_healthz_is_public():
    async def _run():
        client, _ = await _mk_client_with_user()
        try:
            r = await client.get("/healthz")
            assert r.status == 200
            body = await r.json()
            assert body["ok"] is True
        finally:
            await client.close()
    asyncio.run(_run())


# ----------------------------------------------------------------- CLI


def _stdout(fn, *args, **kwargs):
    buf = StringIO()
    with patch("sys.stdout", buf):
        rc = fn(*args, **kwargs)
    return rc, buf.getvalue()


def test_cli_migration_sql_smoke():
    rc, out = _stdout(dashboard_cli.main, ["migration-sql"])
    assert rc == 0
    assert "dashboard_users" in out


def test_cli_demo_end_to_end():
    rc, out = _stdout(dashboard_cli.main, ["demo"])
    assert rc == 0
    assert "enrolled user" in out
    assert "issued OTP code=" in out
    assert "verified OTP" in out
    assert "snapshot summary" in out
