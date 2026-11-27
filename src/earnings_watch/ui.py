"""Attribute owned pages to the host build across supported renderer versions."""

from __future__ import annotations

import html
import inspect

from signin_window import connections as renderer

from .build_identity import BUILD

HUB_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'none'; "
    "form-action 'self'"
)


def renderer_options(factory):
    """Supply host identity only through the renderer's optional public API."""
    identity_type = getattr(renderer, "PageIdentity", None)
    if identity_type is None or "identity" not in inspect.signature(factory).parameters:
        return {}
    return {"identity": identity_type(BUILD.service, BUILD.version, BUILD.source_revision)}


def _identity_footer() -> str:
    service = html.escape(BUILD.service, quote=True)
    version = html.escape(BUILD.version, quote=True)
    revision = html.escape(BUILD.source_revision, quote=True)
    return (
        '<footer id="connection-identity" class="app-footer" '
        f'data-service="{service}" data-version="{version}" '
        f'data-source-revision="{revision}" title="Source revision: {revision}">'
        "<span>Verified issuer dates and documents.</span>"
        f"<details><summary>{service} v{version} · {html.escape(BUILD.short_revision)}</summary>"
        f"<div>Source revision: <code>{revision}</code></div></details></footer>"
    )


def page(title: str, body: str, *, intro: str = "") -> str:
    """Render one host footer, using trusted body composition on older renderers."""
    options = renderer_options(renderer.page)
    if not options:
        service = html.escape(BUILD.service, quote=True)
        version = html.escape(BUILD.version, quote=True)
        revision = html.escape(BUILD.source_revision, quote=True)
        body += (
            "<style>#connection-identity{user-select:text;font-size:.8rem;color:var(--muted);"
            "overflow-wrap:anywhere}#connection-identity summary{font-size:inherit;"
            "font-weight:400;overflow-wrap:anywhere}#connection-identity summary:focus-visible{"
            "outline:2px solid var(--focus);outline-offset:3px}"
            "#connection-identity code{user-select:all;overflow-wrap:anywhere}</style>"
            '<footer id="connection-identity" '
            f'data-service="{service}" data-version="{version}" '
            f'data-source-revision="{revision}" title="Source revision: {revision}">'
            f"<details><summary>{service} v{version} · {html.escape(BUILD.short_revision)}</summary>"
            f"<div>Source revision: <code>{revision}</code></div></details></footer>"
        )
    return renderer.page(title, body, intro=intro, **options)


def hub_page(
    title: str,
    body: str,
    *,
    intro: str = "",
    active_view: str = "upcoming",
    account_label: str = "",
    show_nav: bool = True,
) -> str:
    """Render the owned Earnings Watch web shell with external assets and strict CSP."""
    asset_v = html.escape(f"{BUILD.version}-{BUILD.short_revision}", quote=True)
    tabs = [
        ("upcoming", "Upcoming"),
        ("companies", "Companies"),
        ("digests", "Digests & Alerts"),
        ("calendar", "Calendar"),
    ]
    nav_links = "".join(
        f'<a href="/connections/?view={key}"'
        + (' aria-current="page"' if key == active_view else "")
        + f">{html.escape(label)}</a>"
        for key, label in tabs
    )
    account_html = ""
    if show_nav:
        name_span = (
            f'<span class="account-name">{html.escape(account_label)}</span>'
            if account_label
            else ""
        )
        account_html = (
            f'<div class="account">{name_span}'
            '<form class="inline-form" method="post" action="/connections/logout">'
            '<button type="submit" class="button subtle btn-sm">Sign out</button>'
            "</form></div>"
        )
    nav_html = (
        f'<nav class="app-nav" aria-label="Page sections">{nav_links}</nav>' if show_nav else ""
    )
    intro_html = f'<p class="muted">{html.escape(intro)}</p>' if intro else ""
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="color-scheme" content="light dark">'
        f"<title>{html.escape(title)} · Earnings Watch</title>"
        f'<link rel="stylesheet" href="/connections/assets/app.css?v={asset_v}">'
        f'<script src="/connections/assets/app.js?v={asset_v}" defer></script>'
        "</head><body>"
        '<a class="skip" href="#main">Skip to content</a>'
        '<div class="app-shell">'
        '<header class="app-header">'
        '<a class="brand" href="/connections/"><span class="brand-mark" aria-hidden="true">E</span>Earnings Watch</a>'
        f"{account_html}</header>"
        f"{nav_html}"
        f'<main id="main" tabindex="-1">'
        f'<div class="page-top"><div><div class="eyebrow">Earnings Watch</div><h1>{html.escape(title)}</h1>{intro_html}</div></div>'
        '<div id="status-banner" class="status-banner" role="status" aria-live="polite"></div>'
        f"{body}</main>"
        f"{_identity_footer()}"
        "</div></body></html>"
    )
