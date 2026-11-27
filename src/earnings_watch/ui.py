"""Attribute owned pages to the host build across supported renderer versions."""

from __future__ import annotations

import html
import inspect
from collections.abc import Sequence
from typing import Any

from signin_window import connections as renderer
from signin_window.connections import Suite, SuiteLink

from .build_identity import BUILD

HUB_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'none'; "
    "form-action 'self'"
)

_GRID_ICON = (
    '<svg viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">'
    '<rect x="1" y="1" width="4" height="4" rx="1.2"/>'
    '<rect x="6" y="1" width="4" height="4" rx="1.2"/>'
    '<rect x="11" y="1" width="4" height="4" rx="1.2"/>'
    '<rect x="1" y="6" width="4" height="4" rx="1.2"/>'
    '<rect x="6" y="6" width="4" height="4" rx="1.2"/>'
    '<rect x="11" y="6" width="4" height="4" rx="1.2"/>'
    '<rect x="1" y="11" width="4" height="4" rx="1.2"/>'
    '<rect x="6" y="11" width="4" height="4" rx="1.2"/>'
    '<rect x="11" y="11" width="4" height="4" rx="1.2"/></svg>'
)
_MARK_HUES = 9


def renderer_options(factory):
    """Supply host identity only through the renderer's optional public API."""
    identity_type = getattr(renderer, "PageIdentity", None)
    if identity_type is None or "identity" not in inspect.signature(factory).parameters:
        return {}
    return {"identity": identity_type(BUILD.service, BUILD.version, BUILD.source_revision)}


def _mark(label: str, *, small: bool = False) -> str:
    text = label.strip() or "?"
    hue = sum(text.lower().encode()) % _MARK_HUES
    size = " sm" if small else ""
    hue_class = f" h{hue}" if hue else ""
    return f'<span class="suite-mark{hue_class}{size}" aria-hidden="true">{html.escape(text[0])}</span>'


def _suite_bar(
    account_label: str,
    *,
    show_nav: bool,
    suite: Suite | None,
) -> str:
    current = next((link for link in suite.links if link.current), None) if suite else None
    label = current.label if current else "Earnings Watch"
    subtitle = suite.name if suite else "Connections"
    brand_inner = (
        f'{_mark(label)}<span class="suite-title"><b>{html.escape(label)}</b>'
        f"<small>{html.escape(subtitle)}</small></span>"
    )
    parts = ['<header class="suite-bar"><div class="suite-inner">']
    if show_nav:
        parts.append(f'<a class="suite-brand" href="/">{brand_inner}</a>')
    else:
        parts.append(f'<div class="suite-brand">{brand_inner}</div>')

    if show_nav and suite is not None:
        others = [link for link in suite.links if not link.current]
        if others:
            parts.append(
                '<nav class="suite-nav" aria-label="Your apps"><details class="suite-switch">'
                f'<summary aria-label="Apps">{_GRID_ICON}<span>Apps</span></summary>'
                '<div class="suite-menu">'
            )
            sections: dict[str, list[SuiteLink]] = {}
            for link in suite.links:
                sections.setdefault(link.section, []).append(link)
            for section, links in sections.items():
                if section:
                    parts.append(f"<h2>{html.escape(section)}</h2>")
                parts.append("<ul>")
                for link in links:
                    here = ' aria-current="page"' if link.current else ""
                    description = (
                        f"<small>{html.escape(link.description)}</small>"
                        if link.description
                        else ""
                    )
                    target = html.escape(link.url, quote=True)
                    parts.append(
                        f'<li><a href="{target}"{here}>{_mark(link.label, small=True)}'
                        f"<span><b>{html.escape(link.label)}</b>{description}</span></a></li>"
                    )
                parts.append("</ul>")
            parts.append("</div></details></nav>")

    if show_nav:
        # The application's own label (directory name or verified email) wins over the sign-in name.
        chip_name = account_label.strip() or (suite.account.strip() if suite else "")
        if chip_name:
            parts.append(
                f'<div class="suite-user"><i aria-hidden="true">{html.escape(chip_name[0])}</i>'
                f"<span>{html.escape(chip_name)}</span></div>"
            )
        parts.append(
            '<form class="inline-form" method="post" action="/connections/logout">'
            '<button type="submit" class="button quiet btn-sm">Sign out</button>'
            "</form>"
        )

    parts.append("</div></header>")
    return "".join(parts)


def _endpoint(suite: Suite | None) -> str:
    if suite is None or suite.endpoint is None:
        return ""
    endpoint = suite.endpoint
    return (
        f'<details class="endpoint"><summary>{html.escape(endpoint.title)}</summary>'
        + (f"<p>{html.escape(endpoint.help)}</p>" if endpoint.help else "")
        + f'<div class="address"><code id="connection-suite-endpoint">{html.escape(endpoint.url)}</code>'
        '<button type="button" class="button secondary" data-copy-code="connection-suite-endpoint" '
        'data-copied="Address copied">Copy address</button></div>'
        "</details>"
    )


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
        f"<p>Source revision: <code>{revision}</code></p></details></footer>"
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


def render_coverage_warnings(warnings: Sequence[dict[str, Any]] | None) -> str:
    """Render calm, scannable escaped HTML coverage notices for the Web UI."""
    if not warnings:
        return ""
    count = len(warnings)
    items = "".join(
        f"<li>{html.escape(str(item.get('message') or item.get('ticker') or item.get('source_key') or ''))}</li>"
        for item in warnings[:20]
    )
    overflow = (
        f'<p class="muted small">And {count - 20} more in the full status report.</p>'
        if count > 20
        else ""
    )
    if count <= 2:
        heading = "Coverage notice" if count == 1 else f"Coverage notices ({count})"
        return (
            '<section class="callout warn coverage-warnings" role="status" aria-label="Coverage warnings">'
            f'<div class="coverage-title"><strong>{heading}</strong></div>'
            f'<ul class="coverage-list">{items}</ul></section>'
        )
    return (
        '<section class="callout warn coverage-warnings" role="status" aria-label="Coverage warnings">'
        '<details class="coverage-details">'
        f"<summary>Coverage notices ({count}) · some companies need a closer look</summary>"
        f'<ul class="coverage-list">{items}</ul>{overflow}'
        "</details></section>"
    )


def hub_page(
    title: str,
    body: str,
    *,
    intro: str = "",
    active_view: str = "upcoming",
    account_label: str = "",
    show_nav: bool = True,
    coverage_warnings: Sequence[dict[str, Any]] | None = None,
    suite: Suite | None = None,
) -> str:
    """Render the owned Earnings Watch web shell with external assets and strict CSP."""
    asset_v = html.escape(f"{BUILD.version}-{BUILD.short_revision}", quote=True)
    effective_suite = suite if show_nav else None
    tabs = [
        ("upcoming", "Upcoming"),
        ("companies", "Companies"),
        ("digests", "Digests & alerts"),
        ("calendar", "Calendar"),
    ]
    nav_links = "".join(
        f'<a href="/?view={key}"'
        + (' aria-current="page"' if key == active_view else "")
        + f">{html.escape(label)}</a>"
        for key, label in tabs
    )
    nav_html = (
        f'<nav class="app-nav" aria-label="Page sections">{nav_links}</nav>' if show_nav else ""
    )
    eyebrow_html = "" if effective_suite else '<div class="eyebrow">Earnings Watch</div>'
    intro_html = f'<p class="intro">{html.escape(intro)}</p>' if intro else ""
    endpoint_html = _endpoint(effective_suite)
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="color-scheme" content="light dark">'
        f"<title>{html.escape(title)} · Earnings Watch</title>"
        f'<link rel="stylesheet" href="/connections/assets/app.css?v={asset_v}">'
        f'<script src="/connections/assets/app.js?v={asset_v}" defer></script>'
        "</head><body>"
        '<a class="skip" href="#main">Skip to content</a>'
        f"{_suite_bar(account_label, show_nav=show_nav, suite=effective_suite)}"
        '<div class="app-shell">'
        f"{nav_html}"
        '<main id="main" tabindex="-1">'
        f'<div class="page-top">{eyebrow_html}<h1>{html.escape(title)}</h1>{intro_html}</div>'
        '<div id="status-banner" class="status-banner" role="status" aria-live="polite" hidden></div>'
        f"{render_coverage_warnings(coverage_warnings)}"
        f"{body}"
        f"{endpoint_html}</main>"
        f"{_identity_footer()}"
        "</div></body></html>"
    )
