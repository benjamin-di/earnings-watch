from dataclasses import dataclass
from html.parser import HTMLParser
import inspect
import json

from fastmcp import Client
import httpx
import pytest

from earnings_watch import connections, diagnostics, http_server, mcp_server, ui
from earnings_watch.build_identity import BuildIdentity
from earnings_watch.errors import EarningsError


class Footer(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.identities = []
        self.text = []
        self.in_footer = False
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "footer" and attrs.get("id") == "connection-identity":
            self.identities.append(attrs)
            self.in_footer = True

    def handle_data(self, data):
        if self.in_footer:
            self.text.append(data)

    def handle_endtag(self, tag):
        if tag == "footer":
            self.in_footer = False


@pytest.fixture
def build(monkeypatch):
    identity = BuildIdentity("earnings-watch", "1.2.3", "a" * 40)
    for module in (ui, mcp_server, http_server):
        monkeypatch.setattr(module, "BUILD", identity)
    monkeypatch.setenv("SOURCE_REVISION", "b" * 40)
    monkeypatch.setenv("VERSION", "9.9.9")
    return identity


def assert_footer(source, build):
    footer = Footer(source)
    assert len(footer.identities) == 1
    attrs = footer.identities[0]
    assert attrs["data-service"] == build.service
    assert attrs["data-version"] == build.version
    assert attrs["data-source-revision"] == build.revision
    assert attrs["title"] == "Source revision: " + build.revision
    assert "".join(footer.text) == f"{build.service} v{build.version} · {build.short_revision}"
    assert "user-select:text" in source


def test_owned_page_has_one_selectable_host_footer(build):
    body = '<form method="post" action="/connections/watch"><input name="ticker"></form>'
    result = ui.page("Your companies", body, intro="Your choices")
    assert body in result
    assert "Your choices" in result
    assert_footer(result, build)


def test_legacy_renderer_uses_trusted_body_without_new_keywords(build, monkeypatch):
    actual_page = ui.renderer.page
    calls = []

    def legacy_page(title, body, *, intro=""):
        calls.append(body)
        return actual_page(title, body, intro=intro)

    monkeypatch.setattr(ui.renderer, "page", legacy_page)
    result = ui.page("Example", "<p>Owned content</p>")
    assert len(calls) == 1
    assert calls[0].startswith("<p>Owned content</p><footer")
    assert_footer(result, build)


def test_identity_fields_are_escaped_in_owned_footer(monkeypatch):
    identity = BuildIdentity('service"<&', 'version"<&', 'revision"<&' + "c" * 40)
    monkeypatch.setattr(ui, "BUILD", identity)
    result = ui.page("Example", "<p>Owned content</p>")
    assert_footer(result, identity)
    assert 'data-service="service"<&' not in result
    assert 'data-source-revision="revision"<&' not in result
    assert 'revision"&lt;&amp;' not in result
    assert "&lt;" in result and "&amp;" in result


def test_optional_identity_requires_explicit_factory_support(build, monkeypatch):
    @dataclass(frozen=True)
    class PageIdentity:
        service: str
        version: str
        source_revision: str

    monkeypatch.setattr(ui.renderer, "PageIdentity", PageIdentity, raising=False)

    def supported(*, identity=None):
        return identity

    def old():
        pass

    def variadic(**kwargs):
        pass

    identity = ui.renderer_options(supported)["identity"]
    assert (identity.service, identity.version, identity.source_revision) == (
        build.service,
        build.version,
        build.revision,
    )
    assert ui.renderer_options(old) == {}
    assert ui.renderer_options(variadic) == {}
    monkeypatch.delattr(ui.renderer, "PageIdentity")
    assert ui.renderer_options(supported) == {}


def test_configured_browser_auth_forwards_host_identity_when_supported(settings, build):
    settings.oidc_issuer = "https://identity.example"
    settings.public_url = "https://app.example"
    app = connections.create_app(settings)
    auth = app.earnings_app.state.earnings_auth
    assert auth.config.issuer == settings.oidc_issuer
    assert auth.config.client_id == settings.oidc_client_id
    assert auth.config.cookie_name == "__Host-earnings"
    if "identity" in inspect.signature(connections.BrowserAuth).parameters:
        identity = auth.page_identity
        assert (identity.service, identity.version, identity.source_revision) == (
            build.service,
            build.version,
            build.revision,
        )
    else:
        assert not hasattr(auth, "page_identity")


@pytest.mark.parametrize(
    "path,status,title",
    [
        ("login", 503, "Sign-in is unavailable"),
        ("callback", 403, "We could not confirm your sign-in"),
        ("logout", 200, "You are signed out of Connections"),
    ],
)
async def test_browser_auth_owned_shell_uses_forwarded_identity(
    settings, build, monkeypatch, path, status, title
):
    settings.oidc_issuer = "https://identity.example"
    settings.public_url = "https://app.example"
    settings.allowed_hosts = ["app.example"]
    app = connections.create_app(settings)
    auth = app.earnings_app.state.earnings_auth

    async def unavailable():
        raise OSError("synthetic unavailable provider")

    monkeypatch.setattr(auth.client, "load_server_metadata", unavailable)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=settings.public_url
    ) as client:
        response = await client.request(
            "POST" if path == "logout" else "GET",
            "/connections/" + path,
            headers={"Origin": settings.public_url},
        )
    assert response.status_code == status
    assert title in response.text
    assert "synthetic unavailable provider" not in response.text
    if "identity" in inspect.signature(connections.BrowserAuth).parameters:
        assert_footer(response.text, build)
    else:
        assert Footer(response.text).identities == []


class Auth:
    def identity(self, request):
        return {"sub": "alice", "name": "Example", "claims": {}}

    def origin_ok(self, request):
        return request.headers.get("origin") == "http://localhost"

    def routes(self):
        return []

    def middleware(self, app):
        return app


@pytest.mark.parametrize(
    "outcome,status,title",
    [
        ("home", 200, "The dates and documents"),
        ("denied", 403, "Earnings is unavailable"),
        ("home_failure", 503, "Earnings is temporarily unavailable"),
        ("preview", 200, "Review holdings import"),
        ("conflict", 409, "Please review your change"),
        ("change_failure", 503, "Could not save right now"),
    ],
)
async def test_every_owned_html_outcome_keeps_status_and_one_footer(
    seeded, build, monkeypatch, outcome, status, title
):
    def denied(*args, **kwargs):
        raise EarningsError("unauthorized", "Example <denial>")

    def unavailable(*args, **kwargs):
        raise OSError("private diagnostic must not reach the page")

    if outcome == "denied":
        monkeypatch.setattr(connections, "open_service", denied)
    elif outcome == "home_failure":
        monkeypatch.setattr(connections, "open_service", unavailable)
    elif outcome == "change_failure":
        monkeypatch.setattr(connections.interests, "preview", unavailable)

    app = connections.create_app(seeded, auth=Auth())
    async with (
        app.earnings_app.router.lifespan_context(app.earnings_app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as client,
    ):
        if outcome in {"home", "denied", "home_failure"}:
            response = await client.get("/connections/")
        else:
            # Visiting the home page creates the existing calendar signing key.
            assert (await client.get("/connections/")).status_code == 200
            response = await client.post(
                "/connections/import-preview",
                data={"revision": "-1" if outcome == "conflict" else "0", "companies": "ALPHA"},
                headers={"Origin": "http://localhost"},
            )
    assert response.status_code == status, response.text
    assert title in response.text
    assert "private diagnostic" not in response.text
    assert_footer(response.text, build)
    if outcome in {"home", "preview"}:
        assert response.headers["cache-control"] == "no-store"
    if outcome == "home":
        assert response.headers["referrer-policy"] == "no-referrer"
        assert '<form method="post" action="/connections/watch">' in response.text
    if outcome == "preview":
        assert '<form method="post" action="/connections/import-apply">' in response.text
    if outcome == "denied":
        assert "Example &lt;denial&gt;" in response.text


async def test_native_primary_initialize_reports_host_build_version(seeded, build):
    async with Client(mcp_server.create(seeded), mode="legacy") as client:
        result = await client.initialize()
    assert result.server_info.name == "Earnings Watch"
    assert result.server_info.version == build.version


def test_owned_application_constructors_configure_diagnostics(settings, monkeypatch):
    calls = []
    monkeypatch.setattr(diagnostics, "configure_logging", lambda: calls.append(True))
    mcp_server.create(settings)
    assert calls
    calls.clear()
    http_server.AuthenticatedApp(settings, "gateway")
    assert calls


@pytest.mark.parametrize(
    "path,name", [("/mcp", "Earnings Watch"), ("/monitor/mcp", "Earnings operations")]
)
async def test_http_native_initialize_reports_host_build_version(
    seeded, build, monkeypatch, path, name
):
    monkeypatch.setenv("TEST_BACKEND_TOKEN", "c" * 40)
    seeded.backend_tokens_env = ["TEST_BACKEND_TOKEN"]
    app = http_server.AuthenticatedApp(seeded, "gateway")
    async with (
        app.app.router.lifespan_context(app.app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as client,
    ):
        response = await client.post(
            path,
            headers={
                "Authorization": "Bearer " + "c" * 40,
                "X-Fleet-Person": "alice",
                "Accept": "application/json, text/event-stream",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            },
        )
    assert response.status_code == 200, response.text
    if response.headers["content-type"].startswith("text/event-stream"):
        result = json.loads(
            next(line[6:] for line in response.text.splitlines() if line.startswith("data: "))
        )
    else:
        result = response.json()
    assert result["result"]["serverInfo"] == {"name": name, "version": build.version}
