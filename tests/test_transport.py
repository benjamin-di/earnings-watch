import socket
import httpx
import pytest
from earnings_watch.transport import MAX_BYTES, Transport


def resolver(address):
    return lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))]


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "10.0.0.1", "169.254.169.254", "192.168.1.1", "::1", "fc00::1", "fe80::1"],
)
async def test_dns_refuses_nonpublic_addresses(address):
    calls = []
    transport = Transport(
        {"issuer.example"},
        resolver=resolver(address),
        mock=httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(200)),
    )
    with pytest.raises(ValueError, match="non-public"):
        await transport.get("https://issuer.example/calendar")
    assert not calls


async def test_redirect_rechecks_dns_and_does_not_contact_private_destination():
    calls = []

    def dns(host, *a, **k):
        return resolver("93.184.216.34" if host == "issuer.example" else "10.0.0.1")()

    def handler(request):
        calls.append(request)
        return httpx.Response(302, headers={"location": "https://internal.example/secret"})

    transport = Transport(
        {"issuer.example", "internal.example"},
        resolver=dns,
        mock=httpx.MockTransport(handler),
        pace=0,
    )
    with pytest.raises(ValueError, match="non-public"):
        await transport.get("https://issuer.example/calendar")
    assert len(calls) == 1


async def test_breaker_and_budget():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(429, headers={"retry-after": "60"})

    transport = Transport(
        {"issuer.example"},
        resolver=resolver("93.184.216.34"),
        mock=httpx.MockTransport(handler),
        pace=0,
    )
    with pytest.raises(httpx.HTTPStatusError):
        await transport.get("https://issuer.example/")
    with pytest.raises(ValueError, match="breaker"):
        await transport.get("https://issuer.example/")
    assert len(calls) == 1
    transport = Transport(
        {"issuer.example"},
        daily_budget=1,
        resolver=resolver("93.184.216.34"),
        mock=httpx.MockTransport(lambda r: httpx.Response(200, text="ok")),
        pace=0,
    )
    assert (await transport.get("https://issuer.example/")).body == b"ok"
    with pytest.raises(ValueError, match="budget"):
        await transport.get("https://issuer.example/")


async def test_conditional_and_size_limit():
    transport = Transport(
        {"issuer.example"},
        resolver=resolver("93.184.216.34"),
        mock=httpx.MockTransport(
            lambda r: (
                httpx.Response(304)
                if r.headers.get("if-none-match")
                else httpx.Response(200, content=b"x" * (MAX_BYTES + 1))
            )
        ),
        pace=0,
    )
    assert (
        await transport.get("https://issuer.example/", headers={"If-None-Match": '"example"'})
    ).status == 304
    with pytest.raises(ValueError, match="12 MiB"):
        await transport.get("https://issuer.example/")
