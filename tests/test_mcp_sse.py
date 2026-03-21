from app.main import _should_suppress_asyncio_transport_error


class _FakeHandle:
    def __repr__(self) -> str:
        return "<Handle _ProactorBasePipeTransport._call_connection_lost(None)>"


class TestMcpDiscovery:
    async def test_oauth_discovery_root_is_benign(self, client):
        r = await client.get("/.well-known/oauth-authorization-server")
        assert r.status_code == 200
        body = r.json()
        assert body["issuer"] == "http://test"
        assert body["token_endpoint"] is None
        assert body["token_endpoint_auth_methods_supported"] == ["none"]

    async def test_oauth_discovery_variants_are_benign(self, client):
        for path in (
            "/.well-known/oauth-authorization-server/mcp/sse",
            "/mcp/sse/.well-known/oauth-authorization-server",
        ):
            r = await client.get(path)
            assert r.status_code == 200
            assert r.json()["issuer"] == "http://test"


class TestAsyncioNoiseFilter:
    def test_suppresses_winerror_10054_connection_lost_noise(self):
        exc = ConnectionResetError(10054, "remote host closed connection")
        exc.winerror = 10054
        context = {
            "message": "Exception in callback _ProactorBasePipeTransport._call_connection_lost(None)",
            "exception": exc,
            "handle": _FakeHandle(),
        }
        assert _should_suppress_asyncio_transport_error(context) is True

    def test_keeps_other_asyncio_errors_visible(self):
        context = {
            "message": "Exception in callback some_other_handler",
            "exception": RuntimeError("boom"),
        }
        assert _should_suppress_asyncio_transport_error(context) is False
