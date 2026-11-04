"""Attribute owned pages to the host build across supported renderer versions."""

import html
import inspect

from signin_window import connections as renderer

from .build_identity import BUILD


def renderer_options(factory):
    """Supply host identity only through the renderer's optional public API."""
    identity_type = getattr(renderer, "PageIdentity", None)
    if identity_type is None or "identity" not in inspect.signature(factory).parameters:
        return {}
    return {"identity": identity_type(BUILD.service, BUILD.version, BUILD.revision)}


def page(title: str, body: str, *, intro: str = "") -> str:
    """Render one host footer, using trusted body composition on older renderers."""
    options = renderer_options(renderer.page)
    if not options:
        service = html.escape(BUILD.service, quote=True)
        version = html.escape(BUILD.version, quote=True)
        revision = html.escape(BUILD.revision, quote=True)
        body += (
            '<footer id="connection-identity" style="user-select:text;font-size:.8rem" '
            f'data-service="{service}" data-version="{version}" '
            f'data-source-revision="{revision}" title="Source revision: {revision}">'
            f"{service} v{version} · {html.escape(BUILD.short_revision)}</footer>"
        )
    return renderer.page(title, body, intro=intro, **options)
