"""Connections UI, private calendar subscriptions, and read-only internal targets API."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import hmac
import html
from importlib import resources
import json
import os
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from issuer_sources.model import (
    IrUrls,
    LibraryTargetsPayload,
    PeriodScheme,
    PersonScope,
    TargetCompany,
    VerifiedDocument,
    normalize_document_category,
)
from signin_window.oidc import BrowserAuth, OIDCConfig
from starlette.applications import Starlette
from starlette.datastructures import UploadFile
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import BaseRoute, Mount, Route

from . import interests, notify
from .composition import open_service
from .errors import EarningsError
from .http_server import AuthenticatedApp
from .presentation import format_event_summary, format_utc_slot, group_upcoming
from .render import _safe_url, render_email
from .store import accounts
from .store.database import Catalogue, LibraryReader, PersonalStore, integrity, people
from .transport import IdentityDirectory
from .ui import HUB_CSP, hub_page, renderer_options

MAX_BODY = 65536
_VIEWS = frozenset({"upcoming", "companies", "digests", "calendar", "preview"})
_FILTERS = frozenset({"all", "holdings", "watchlist", "earnings"})
_STATIC_ASSETS = {
    "app.css": "text/css; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
}
_COMMON_TIMEZONES = (
    "UTC",
    "America/New_York",
    "America/Chicago",
    "America/Los_Angeles",
    "Europe/London",
    "Europe/Berlin",
    "Europe/Zurich",
    "Europe/Paris",
    "Asia/Hong_Kong",
    "Asia/Singapore",
    "Asia/Tokyo",
    "Australia/Sydney",
)
_ALL_IANA_TIMEZONES = tuple(
    sorted(
        tz
        for tz in available_timezones()
        if "/" in tz and not tz.startswith("Etc/") and tz not in _COMMON_TIMEZONES
    )
)


def _is_windows_client(headers) -> bool:
    platform = headers.get("sec-ch-ua-platform", "").strip().strip('"').lower()
    if platform:
        return platform == "windows"
    return "windows" in headers.get("user-agent", "").lower()


def _render_timezone_select(selected_tz: str) -> str:
    def _opt(tz: str) -> str:
        sel = " selected" if tz == selected_tz else ""
        esc = html.escape(tz, quote=True)
        return f'<option value="{esc}"{sel}>{html.escape(tz)}</option>'

    common = list(_COMMON_TIMEZONES)
    if (
        selected_tz
        and selected_tz not in _COMMON_TIMEZONES
        and selected_tz not in _ALL_IANA_TIMEZONES
    ):
        common.insert(0, selected_tz)
    common_opts = "".join(_opt(tz) for tz in common)
    all_opts = "".join(_opt(tz) for tz in _ALL_IANA_TIMEZONES)
    return (
        '<select id="pref-tz" name="timezone" required>'
        f'<optgroup label="Common">{common_opts}</optgroup>'
        f'<optgroup label="All timezones">{all_opts}</optgroup>'
        "</select>"
    )


def _render_hour_select(selected_hour: int) -> str:
    options = "".join(
        f'<option value="{h}"'
        + (" selected" if h == selected_hour else "")
        + f">{h:02d}:00</option>"
        for h in range(24)
    )
    return f'<select id="pref-hour" name="hour" required>{options}</select>'


def _wants_json(request) -> bool:
    return "application/json" in request.headers.get("accept", "").lower()


def _hub_html(content: str, status_code: int = 200) -> HTMLResponse:
    return HTMLResponse(
        content,
        status_code=status_code,
        headers={
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": HUB_CSP,
        },
    )


def _valid_bearer(headers: dict[str, str], env_names: list[str]) -> bool:
    auth_header = headers.get("authorization", "")
    token = auth_header[7:] if auth_header.startswith("Bearer ") else ""
    if len(token) < 32:
        return False
    configured = [os.environ.get(name, "") for name in env_names]
    return any(len(t) >= 32 and hmac.compare_digest(token.encode(), t.encode()) for t in configured)


def _build_company_queries(name: str, ticker: str) -> list[str]:
    queries: list[str] = []
    for candidate in (name.strip(), ticker.strip()):
        if candidate and candidate not in queries:
            queries.append(candidate)
    return queries


async def _targets_payload(settings, directory) -> dict:
    generated_at = datetime.now(timezone.utc).isoformat()
    result_people: list[dict] = []
    for person_row in await asyncio.to_thread(people, settings.db):
        subject = person_row["id"]
        if directory is not None:
            info = await directory.resolve(subject)
            if not info["admitted"] or info["disabled"]:
                continue
            email = str(info.get("email") or "")
            portfolio_admitted = bool(info.get("portfolio_admitted") is True)
        else:
            email = await asyncio.to_thread(accounts.remembered_email, settings.db, subject)
            portfolio_admitted = False
        prof = await asyncio.to_thread(accounts.profile, settings.db, subject)
        prefs = prof["settings"]
        follows = await asyncio.to_thread(lambda: PersonalStore(settings.db, subject).follows())
        if directory is None:
            portfolio_admitted = any(r["origin"].startswith("holdings:") for r in follows)
        muted = await asyncio.to_thread(accounts.mutes, settings.db, subject)
        news_mutes = set(prefs.get("news_mutes") or [])
        by_pair: dict[tuple[str, str], dict] = {}
        for row in follows:
            is_holding = row["origin"].startswith("holdings:")
            if is_holding and not portfolio_admitted:
                continue
            origin = "holding" if is_holding else "watchlist"
            ticker = row["ticker"]
            exchange = row["exchange"]
            hints = json.loads(row.get("hints") or "{}")
            name = (row.get("company_name") or hints.get("name") or ticker).strip() or ticker
            cal_muted = (ticker, exchange) in muted or (ticker, "") in muted
            news_muted = f"{ticker}:{exchange}" in news_mutes or f"{ticker}:" in news_mutes
            entry = {
                "ticker": ticker,
                "exchange": exchange,
                "name": name,
                "queries": _build_company_queries(name, ticker),
                "origin": origin,
                "state": row["state"],
                "calendar": not cal_muted,
                "news": not news_muted,
            }
            pair = (ticker, exchange)
            existing = by_pair.get(pair)
            if existing is None or (origin == "holding" and existing["origin"] != "holding"):
                by_pair[pair] = entry
        scope = str(prefs.get("news_scope") or "both")
        if not portfolio_admitted:
            scope = "watchlist"
        result_people.append(
            {
                "person": subject,
                "email": email,
                "timezone": prof["timezone"],
                "portfolio_admitted": portfolio_admitted,
                "news_digest": {
                    "enabled": bool(prefs.get("news_digest", False)),
                    "cadence": str(prefs.get("news_cadence") or "twice_daily"),
                    "weekdays_only": bool(prefs.get("news_weekdays_only", False)),
                    "pause_until": str(prefs.get("news_pause_until") or ""),
                    "scope": scope,
                },
                "companies": list(by_pair.values()),
            }
        )
    return {"schema": 1, "generated_at": generated_at, "people": result_people}


async def _library_targets_payload(settings, directory, *, schema_version: int = 1) -> dict:
    generated_at = datetime.now(timezone.utc).isoformat()
    library = LibraryReader(settings.db)
    sources_by_key = {source.key: source for source in library.sources()}
    event_to_source = {ev["id"]: ev["issuer_key"] for ev in library.events()}
    docs_by_source: dict[str, list[dict]] = {}
    for doc in library.documents():
        if doc.get("verified") is not True:
            continue
        source_key = event_to_source.get(str(doc.get("event_id") or ""))
        if not source_key:
            continue
        role_val = str(doc.get("role") or "")
        entry = {
            "id": str(doc.get("id") or ""),
            "event_id": str(doc.get("event_id") or ""),
            "url": str(doc.get("url") or ""),
            "title": str(doc.get("title") or ""),
            "role": role_val,
            "first_seen": str(doc.get("first_seen") or ""),
        }
        if schema_version == 2:
            entry["category"] = normalize_document_category(role_val).value
        docs_by_source.setdefault(source_key, []).append(entry)

    active_source_keys: set[str] = set()
    scopes: list[dict] = []
    for person_row in await asyncio.to_thread(people, settings.db):
        subject = person_row["id"]
        if directory is not None:
            info = await directory.resolve(subject)
            if not info["admitted"] or info["disabled"]:
                continue
            portfolio_admitted = bool(info.get("portfolio_admitted") is True)
        else:
            portfolio_admitted = False
        follows = await asyncio.to_thread(lambda: PersonalStore(settings.db, subject).follows())
        if directory is None:
            portfolio_admitted = any(r["origin"].startswith("holdings:") for r in follows)
        person_keys: set[str] = set()
        for row in follows:
            if row["origin"].startswith("holdings:") and not portfolio_admitted:
                continue
            source_key = row.get("source_key")
            if source_key and source_key in sources_by_key:
                person_keys.add(source_key)
                active_source_keys.add(source_key)
        scopes.append({"person": subject, "source_keys": sorted(person_keys)})

    if schema_version == 2:
        v2_companies = [
            TargetCompany(
                source_key=source.key,
                ticker=source.ticker,
                exchange=source.exchange or "",
                name=source.name,
                isin=source.isin,
                sec_cik=source.sec_cik,
                hkex_stock_id=source.hkex_stock_id,
                lei=source.lei,
                fy_end_month=source.fy_end_month,
                period_scheme=PeriodScheme(source.period_scheme.value),
                ir_urls=IrUrls(
                    calendar=source.calendar_url,
                    results=source.results_url,
                    announcements=source.announcements_url,
                ),
                verified_documents=[
                    VerifiedDocument.model_validate(d) for d in docs_by_source.get(source.key, [])
                ],
            )
            for source_key in sorted(active_source_keys)
            for source in (sources_by_key[source_key],)
        ]
        v2_payload = LibraryTargetsPayload(
            schema=2,
            generated_at=generated_at,
            companies=v2_companies,
            scopes=[PersonScope.model_validate(s) for s in scopes],
        )
        return v2_payload.model_dump(by_alias=True, mode="json")

    companies: list[dict] = []
    for source_key in sorted(active_source_keys):
        source = sources_by_key[source_key]
        ir_urls: list[str] = []
        for raw_url in (source.calendar_url, source.results_url, source.announcements_url):
            url = (raw_url or "").strip()
            if url and url not in ir_urls:
                ir_urls.append(url)
        companies.append(
            {
                "source_key": source.key,
                "ticker": source.ticker,
                "exchange": source.exchange or "",
                "name": source.name,
                "isin": source.isin,
                "sec_cik": source.sec_cik,
                "hkex_stock_id": source.hkex_stock_id,
                "lei": source.lei,
                "ir_urls": ir_urls,
                "verified_documents": docs_by_source.get(source.key, []),
            }
        )

    return {
        "schema": 1,
        "generated_at": generated_at,
        "companies": companies,
        "scopes": scopes,
    }


def _merge_companies(follows: list[dict], *, show_holdings: bool) -> list[dict]:
    """Merge overlapping holding/watchlist rows by (ticker, exchange) into one UI row."""
    merged: dict[tuple[str, str], dict] = {}
    for row in follows:
        is_holding = row["origin"].startswith("holdings:")
        if is_holding and not show_holdings:
            continue
        pair = (row["ticker"], row["exchange"])
        entry = merged.get(pair)
        if entry is None:
            entry = {
                "ticker": row["ticker"],
                "exchange": row["exchange"],
                "company_name": row.get("company_name") or row["ticker"],
                "source_key": row.get("source_key"),
                "state": row["state"],
                "reason": row.get("reason") or "",
                "is_holding": False,
                "is_watchlist": False,
                "holding_feeds": set(),
            }
            merged[pair] = entry
        if is_holding:
            entry["is_holding"] = True
            entry["holding_feeds"].add(row["origin"].removeprefix("holdings:"))
        else:
            entry["is_watchlist"] = True
        if row.get("company_name") and entry["company_name"] == entry["ticker"]:
            entry["company_name"] = row["company_name"]
        if row.get("source_key") and not entry["source_key"]:
            entry["source_key"] = row["source_key"]
        if row["state"] == "registered":
            entry["state"] = "registered"
        if row.get("reason") and not entry["reason"]:
            entry["reason"] = row["reason"]
    return sorted(
        merged.values(),
        key=lambda item: (
            not item["is_holding"],
            item["company_name"].lower(),
            item["ticker"],
            item["exchange"],
        ),
    )


def create_app(settings, mode="gateway", *, auth=None, directory=None):
    if directory is None and settings.directory_url:
        directory = IdentityDirectory(
            settings.directory_url, os.environ.get(settings.directory_key_env, "")
        )
    if settings.directory_required and directory is None:
        raise ValueError("The identity directory is required")
    if auth is None and settings.oidc_issuer:
        auth = BrowserAuth(
            OIDCConfig(
                issuer=settings.oidc_issuer,
                client_id=settings.oidc_client_id,
                public_url=settings.public_url,
                state_dir=settings.data_dir / "browser",
                required_group=settings.oidc_required_group,
                cookie_name="__Host-earnings",
                claim_names=("person_id", "email", "email_verified"),
            ),
            **renderer_options(BrowserAuth),
        )
    native = AuthenticatedApp(settings, mode, directory=directory)

    async def identity(request):
        who = auth.identity(request) if auth else None
        if not who:
            return None, None
        subject = who["sub"]
        info = (
            await directory.resolve(subject)
            if directory
            else {
                "id": subject,
                "admitted": True,
                "disabled": False,
                "portfolio_admitted": False,
                "displayName": who.get("name", ""),
                "email": who.get("claims", {}).get("email", "")
                if who.get("claims", {}).get("email_verified") is True
                else "",
            }
        )
        if not info["admitted"] or info["disabled"]:
            raise EarningsError("unauthorized", "Your account no longer has access to Earnings.")
        accounts.provision(
            settings.db, subject, who.get("claims", {}).get("person_id", "") or who.get("name", "")
        )
        if not directory and info.get("email"):
            accounts.remember_email(settings.db, subject, info["email"])
        return subject, info

    def _is_portfolio_admitted(info: dict, follows: list[dict]) -> bool:
        if directory is not None:
            return bool(info.get("portfolio_admitted") is True)
        return any(r["origin"].startswith("holdings:") for r in follows)

    def form_fields(profile, *, view: str = ""):
        fields = '<input type="hidden" name="revision" value="' + str(profile["revision"]) + '">'
        if view in _VIEWS:
            fields += '<input type="hidden" name="view" value="' + view + '">'
        return fields

    def button(label: str, *, cls: str = "button") -> str:
        class_attr = f' class="{cls}"' if cls else ""
        return f'<button type="submit"{class_attr}>{html.escape(label)}</button>'

    def _suite_for(request):
        if auth is None or not hasattr(auth, "suite"):
            return None
        return auth.suite(request)

    def _certainty_badge(event_row: dict) -> str:
        status = event_row.get("status", "")
        cls = "badge ok" if status == "ir-confirmed" else "badge"
        return f'<span class="{cls}">{html.escape(str(event_row.get("certainty") or ""))}</span>'

    def _render_event_card(event_row: dict, changed: set[str], *, show_holdings: bool) -> str:
        doc_links = "".join(
            '<a href="'
            + html.escape(safe_doc_url, quote=True)
            + '" rel="noreferrer">'
            + html.escape(d.get("title") or d["role"])
            + "</a>"
            for d in event_row.get("documents", [])
            if (safe_doc_url := _safe_url(str(d.get("url") or "")))
        )
        changed_html = (
            '<span class="pill pill-accent" data-badge="changed">Changed</span>'
            if event_row["id"] in changed
            else ""
        )
        company_name = event_row.get("company_name") or event_row["ticker"]
        ticker = event_row["ticker"]
        ticker_label = (
            f' <span class="company-ticker">{html.escape(ticker)}</span>'
            if company_name != ticker
            else ""
        )
        summary = format_event_summary(
            str(event_row.get("period") or ""), str(event_row.get("type") or "")
        ) or str(event_row.get("title") or "")
        why = [w for w in event_row.get("why_followed", []) if show_holdings or w != "holding"]
        origin_badges = "".join(
            f'<span class="pill">{html.escape("Holding" if w == "holding" else "Watchlist")}</span>'
            for w in why
        )
        safe_source_url = _safe_url(str(event_row.get("source_url") or ""))
        source_link = (
            f'<a href="{html.escape(safe_source_url, quote=True)}" rel="noreferrer">Source</a>'
            if safe_source_url
            else ""
        )
        return (
            '<li class="event-card"><div class="event-main">'
            '<div class="event-head">'
            f'<span class="event-company">{html.escape(company_name)}</span>{ticker_label}'
            f'<span class="event-summary">{html.escape(summary)}</span>'
            f"{changed_html}"
            "</div>"
            '<div class="event-meta">'
            f"<span>{html.escape(event_row['when'])}</span>"
            f"{_certainty_badge(event_row)}"
            f"{origin_badges}"
            "</div></div>"
            '<div class="event-links">'
            f"{source_link}"
            f"{doc_links}"
            "</div></li>"
        )

    def _render_upcoming_tab(
        service, profile, changed: set[str], *, show_holdings: bool, filter_by: str
    ) -> str:
        try:
            local_today = datetime.now(ZoneInfo(profile["timezone"])).date()
        except (ValueError, ZoneInfoNotFoundError):
            local_today = datetime.now(timezone.utc).date()
        current = service.upcoming(days=90, include_holdings=show_holdings)
        recent_data = service.recent(days=14, include_holdings=show_holdings)

        def _visible(ev: dict) -> bool:
            why = ev.get("why_followed") or []
            if not show_holdings and why == ["holding"]:
                return False
            if filter_by == "holdings":
                return show_holdings and "holding" in why
            if filter_by == "watchlist":
                return "watchlist" in why
            if filter_by == "earnings":
                return ev.get("type") == "earnings"
            return True

        upcoming_events = [e for e in current["events"] if _visible(e)]
        recent_events = [e for e in recent_data["events"] if _visible(e)]
        visible_changed = changed & (
            {e["id"] for e in upcoming_events} | {e["id"] for e in recent_events}
        )
        grouped = group_upcoming(upcoming_events, today=local_today)

        parts: list[str] = []
        note = current.get("coverage_note", "")
        if note:
            parts.append('<p class="callout info" role="status">' + html.escape(note) + "</p>")

        pills = [("all", "All")]
        if show_holdings:
            pills.append(("holdings", "Holdings"))
        pills.extend([("watchlist", "Watchlist"), ("earnings", "Earnings")])
        pill_html = "".join(
            '<a class="filter-pill" href="/connections/?view=upcoming'
            + (f"&filter={key}" if key != "all" else "")
            + '"'
            + (' aria-current="true"' if key == filter_by else "")
            + f">{html.escape(label)}</a>"
            for key, label in pills
        )
        header_actions = ""
        if visible_changed:
            header_actions = (
                '<form class="hub-inline-form" method="post" action="/connections/mark-seen" data-async="mark-seen">'
                + form_fields(profile, view="upcoming")
                + button("Mark changes seen", cls="button secondary btn-sm")
                + "</form>"
            )
        parts.append(
            f'<div class="filter-pills" role="navigation" aria-label="Filter events">{pill_html}{header_actions}</div>'
        )
        parts.append(
            f'<p class="muted small">Next 90 days · {html.escape(profile["timezone"])} · '
            '<a href="/connections/?view=digests">Change timezone</a></p>'
        )

        group_defs = [
            ("this_week", "This week"),
            ("next_week", "Next week"),
            ("later", "Later"),
        ]
        any_upcoming = False
        for bucket_key, bucket_label in group_defs:
            bucket = grouped[bucket_key]
            if not bucket:
                continue
            any_upcoming = True
            cards = "".join(
                _render_event_card(e, visible_changed, show_holdings=show_holdings) for e in bucket
            )
            parts.append(
                f'<section><h2 class="group-title">{bucket_label} ({len(bucket)})</h2>'
                f'<ul class="event-list">{cards}</ul></section>'
            )
        if not any_upcoming:
            parts.append(
                '<section class="empty"><p>No upcoming events match this view.</p>'
                '<p><a class="button primary" href="/connections/?view=companies">Add a company</a></p></section>'
            )

        if recent_events:
            recent_cards = "".join(
                _render_event_card(e, visible_changed, show_holdings=show_holdings)
                for e in recent_events
            )
            parts.append(
                f'<details class="section-details"><summary>Recent releases (14 days) · {len(recent_events)}</summary>'
                f'<ul class="event-list">{recent_cards}</ul></details>'
            )
        else:
            parts.append(
                '<details class="section-details"><summary>Recent releases (14 days) · 0</summary>'
                '<p class="muted small">No releases in the last 14 days.</p></details>'
            )
        return "".join(parts)

    def _render_companies_tab(
        service, profile, follows: list[dict], *, show_holdings: bool, news_enabled: bool
    ) -> str:
        muted = accounts.mutes(settings.db, profile["person"])
        news_mutes = set(profile["settings"].get("news_mutes") or [])
        upcoming_by_source: dict[str, dict] = {}
        for ev in service.upcoming(days=90, include_holdings=show_holdings)["events"]:
            upcoming_by_source.setdefault(ev["issuer_key"], ev)

        options = "".join(
            '<option value="'
            + html.escape(s.ticker, quote=True)
            + '">'
            + html.escape(s.name + " · " + s.ticker + " · " + (s.exchange or ""))
            + "</option>"
            for s in LibraryReader(settings.db).sources()
        )
        allow_import = directory is None or show_holdings
        import_button = (
            '<button type="button" class="button" popovertarget="import-csv-popover">Import CSV</button>'
            if allow_import
            else ""
        )
        import_popover = (
            '<div id="import-csv-popover" popover>'
            "<h2>Import companies from CSV</h2>"
            '<p class="muted small">Paste tickers or MIC:TICKER, or upload a CSV with ticker and exchange columns. Quantities and values are discarded. Review changes before replacing uploaded holdings.</p>'
            '<form class="stack" method="post" action="/connections/import-preview" enctype="multipart/form-data">'
            + form_fields(profile, view="companies")
            + '<div class="field"><label for="import-companies">Companies</label>'
            '<textarea id="import-companies" name="companies" rows="4" maxlength="32768"></textarea></div>'
            '<div class="field"><label for="import-file">CSV file</label>'
            '<input id="import-file" name="file" type="file" accept=".csv,text/csv"></div>'
            '<div class="actions-row">'
            + button("Preview import", cls="button primary")
            + '<button type="button" class="button" popovertarget="import-csv-popover" popovertargetaction="hide">Cancel</button>'
            "</div></form></div>"
            if allow_import
            else ""
        )
        request_company_popover = (
            '<div id="request-company-popover" popover>'
            "<h2>Track a new company</h2>"
            '<p class="muted small">Enter any stock ticker or ISIN to start tracking a company not yet in the catalogue. Automatic discovery will find its official investor-relations calendar.</p>'
            '<form class="stack" method="post" action="/connections/watch" data-async="reload">'
            + form_fields(profile, view="companies")
            + '<div class="field"><label for="req-ticker">Ticker or ISIN</label>'
            '<input id="req-ticker" name="ticker" required maxlength="100" autocomplete="off" placeholder="e.g. NVDA, 7203.T, or US67066G1040"></div>'
            '<div class="field"><label for="req-exchange">Exchange or MIC (optional)</label>'
            '<input id="req-exchange" name="exchange" maxlength="12" autocomplete="off" placeholder="e.g. XNAS, NYSE, XHKG"></div>'
            '<div class="field"><label for="req-name">Company name (optional)</label>'
            '<input id="req-name" name="name" maxlength="120" autocomplete="off" placeholder="e.g. NVIDIA Corporation"></div>'
            '<div class="field"><label for="req-ir-url">Official IR URL hint (optional)</label>'
            '<input id="req-ir-url" name="ir_url" type="url" maxlength="500" autocomplete="off" placeholder="https://investor.example.com/events"></div>'
            '<div class="form-error" role="alert"></div>'
            '<div class="actions-row">'
            + button("Queue company for tracking", cls="button primary")
            + '<button type="button" class="button" popovertarget="request-company-popover" popovertargetaction="hide">Cancel</button>'
            "</div></form></div>"
        )
        quick_add = (
            '<section class="panel" aria-label="Add company">'
            '<form class="toolbar" method="post" action="/connections/watch" data-async="reload">'
            + form_fields(profile, view="companies")
            + '<div class="field"><label for="watch-ticker">Company ticker, name, or ISIN</label>'
            '<input id="watch-ticker" name="ticker" list="companies" required maxlength="100" autocomplete="off" placeholder="e.g. NVDA, 7203.T, or company name">'
            f'<datalist id="companies">{options}</datalist></div>'
            '<div class="field field-narrow"><label for="watch-exchange">Exchange (optional)</label>'
            '<input id="watch-exchange" name="exchange" maxlength="12" autocomplete="off" placeholder="e.g. XNAS, NYSE"></div>'
            + button("Add to watchlist", cls="button primary")
            + '<button type="button" class="button" popovertarget="request-company-popover">Request new company</button>'
            + import_button
            + '<div class="form-error" role="alert"></div>'
            "</form>"
            '<p class="help">Not tracked yet? Type any ticker (e.g. <code>NVDA</code>, <code>7203.T</code>, <code>NASDAQ:NVDA</code>) or ISIN above, or use <strong>Request new company</strong> to include a company name or official IR URL.</p>'
            "</section>" + request_company_popover + import_popover
        )

        merged_rows = _merge_companies(follows, show_holdings=show_holdings)
        holding_count = sum(1 for r in merged_rows if r["is_holding"])
        watchlist_count = sum(1 for r in merged_rows if r["is_watchlist"])
        sync_dates = sorted(
            {
                profile["holdings_sync"][feed][:10]
                for r in merged_rows
                for feed in r["holding_feeds"]
                if feed != "upload" and profile["holdings_sync"].get(feed)
            }
        )

        summary_bits: list[str] = []
        if show_holdings:
            sync_note = f" (synced {sync_dates[-1]})" if sync_dates else ""
            summary_bits.append(f"{holding_count} portfolio holdings{sync_note}")
        summary_bits.append(f"{watchlist_count} watchlist")
        summary_line = " · ".join(summary_bits)

        filter_opts = ['<option value="all">All companies</option>']
        if show_holdings:
            filter_opts.append('<option value="holding">Portfolio holdings</option>')
        muted_filter_label = "Muted (calendar or news)" if news_enabled else "Muted"
        filter_opts.extend(
            [
                '<option value="watchlist">Watchlist</option>',
                f'<option value="muted">{muted_filter_label}</option>',
            ]
        )

        row_items: list[str] = []
        for item in merged_rows:
            ticker = item["ticker"]
            exchange = item["exchange"]
            name = item["company_name"]
            exact = (ticker, exchange) in muted
            every = (ticker, "") in muted
            is_cal_muted = exact or every
            cal_target = "" if every and not exact else exchange
            cal_action = "unmute" if is_cal_muted else "mute"
            cal_label = (
                "Unmute all exchanges"
                if (is_cal_muted and every and not exact)
                else ("Calendar: Off" if is_cal_muted else "Calendar: On")
            )
            cal_cls = (
                "button toggle-btn toggle-off" if is_cal_muted else "button toggle-btn toggle-on"
            )
            cal_form = (
                f'<form class="hub-inline-form" method="post" action="/connections/{cal_action}">'
                + form_fields(profile, view="companies")
                + f'<input type="hidden" name="ticker" value="{html.escape(ticker, quote=True)}">'
                + f'<input type="hidden" name="exchange" value="{html.escape(cal_target if is_cal_muted else exchange, quote=True)}">'
                + f'<button type="submit" class="{cal_cls}" data-async-btn="toggle">{html.escape(cal_label)}</button>'
                + "</form>"
            )

            is_news_muted = False
            news_form = ""
            if news_enabled:
                news_exact = f"{ticker}:{exchange}" in news_mutes
                news_every = f"{ticker}:" in news_mutes
                is_news_muted = news_exact or news_every
                news_target = "" if news_every and not news_exact else exchange
                news_action = "news-unmute" if is_news_muted else "news-mute"
                news_label = (
                    "Unmute all exchanges"
                    if (is_news_muted and news_every and not news_exact)
                    else ("News: Off" if is_news_muted else "News: On")
                )
                news_cls = (
                    "button toggle-btn toggle-off"
                    if is_news_muted
                    else "button toggle-btn toggle-on"
                )
                news_form = (
                    f'<form class="hub-inline-form" method="post" action="/connections/{news_action}">'
                    + form_fields(profile, view="companies")
                    + f'<input type="hidden" name="ticker" value="{html.escape(ticker, quote=True)}">'
                    + f'<input type="hidden" name="exchange" value="{html.escape(news_target if is_news_muted else exchange, quote=True)}">'
                    + f'<button type="submit" class="{news_cls}" data-async-btn="toggle">{html.escape(news_label)}</button>'
                    + "</form>"
                )

            remove_form = ""
            if item["is_watchlist"]:
                remove_form = (
                    '<form class="hub-inline-form" method="post" action="/connections/unwatch">'
                    + form_fields(profile, view="companies")
                    + f'<input type="hidden" name="ticker" value="{html.escape(ticker, quote=True)}">'
                    + f'<input type="hidden" name="exchange" value="{html.escape(exchange, quote=True)}">'
                    + '<button type="submit" class="button subtle btn-sm" data-async-btn="remove">Remove</button>'
                    + "</form>"
                )

            badges: list[str] = []
            if item["is_holding"]:
                badges.append('<span class="pill">Holding</span>')
            if item["is_watchlist"]:
                badges.append('<span class="pill" data-badge="watchlist">Watchlist</span>')
            if item["state"] == "queued":
                badges.append('<span class="badge warn">Discovering…</span>')

            ev = upcoming_by_source.get(item["source_key"]) if item.get("source_key") else None
            if ev:
                ev_summary = (
                    format_event_summary(str(ev.get("period") or ""), str(ev.get("type") or ""))
                    or ev["period"]
                )
                sub_text = f"Next: {ev['when']} · {ev_summary}"
            elif item["state"] == "queued" and item["reason"]:
                sub_text = item["reason"]
            elif item["state"] == "queued":
                sub_text = "Queued for official IR calendar discovery"
            else:
                sub_text = "No upcoming events in the next 90 days"

            ticker_mic = f"{ticker} · {exchange}" if exchange else ticker
            search_blob = f"{name} {ticker} {exchange}".strip()
            muted_news_attr = (
                f' data-muted-news="{str(is_news_muted).lower()}"' if news_enabled else ""
            )
            row_items.append(
                '<div class="company-row" '
                f'data-search="{html.escape(search_blob, quote=True)}" '
                f'data-holding="{str(item["is_holding"]).lower()}" '
                f'data-watchlist="{str(item["is_watchlist"]).lower()}" '
                f'data-muted-cal="{str(is_cal_muted).lower()}"'
                f"{muted_news_attr}>"
                '<div class="company-main">'
                '<div class="company-head">'
                f'<span class="company-name">{html.escape(name)}</span>'
                f'<span class="company-ticker">{html.escape(ticker_mic)}</span>'
                f"{''.join(badges)}"
                "</div>"
                f'<p class="company-sub">{html.escape(sub_text)}</p>'
                "</div>"
                f'<div class="company-controls">{cal_form}{news_form}{remove_form}</div>'
                "</div>"
            )

        if row_items:
            list_block = (
                '<div class="toolbar-inline">'
                '<label class="sr-only" for="company-search">Search companies</label>'
                '<input id="company-search" type="search" placeholder="Search by company, ticker, or exchange…" autocomplete="off">'
                '<label class="sr-only" for="company-filter">Filter companies</label>'
                f'<select id="company-filter">{"".join(filter_opts)}</select>'
                f'<span id="company-count" class="muted small">{len(row_items)} companies</span>'
                "</div>"
                f'<div class="company-list">{"".join(row_items)}</div>'
                '<p id="company-no-match" class="empty" hidden>No companies match your filter.</p>'
            )
        else:
            list_block = '<section class="empty"><p>No companies tracked yet. Add a ticker or company name above to get started.</p></section>'

        return (
            quick_add
            + '<section aria-label="Tracked companies">'
            + '<div class="panel-head"><h2>Tracked companies</h2>'
            + f'<span class="muted small">{html.escape(summary_line)}</span></div>'
            + list_block
            + "</section>"
        )

    def _render_digests_tab(profile, info: dict, *, show_holdings: bool, news_enabled: bool) -> str:
        prefs = profile["settings"]
        tz_name = profile["timezone"]
        try:
            local_today = datetime.now(ZoneInfo(tz_name)).date()
        except (ValueError, ZoneInfoNotFoundError):
            local_today = datetime.now(timezone.utc).date()
        email_label = info.get("email") or "No email is available from your identity provider"

        alert_toggles = "".join(
            '<label class="check"><input type="checkbox" name="'
            + key
            + '"'
            + (" checked" if prefs[key] else "")
            + "> "
            + html.escape(label)
            + "</label>"
            for key, label in [
                ("results", "Results out"),
                ("brief", "Daily brief, only when useful"),
                ("weekly_brief", "Weekly brief"),
                ("urgent", "Urgent date changes"),
            ]
        )
        alerts_card = (
            '<article class="panel">'
            '<div class="card-head"><h2>Earnings alerts</h2></div>'
            '<p class="muted small">Sent to '
            + html.escape(email_label)
            + " (your sign-in email).</p>"
            '<div class="stack">'
            + alert_toggles
            + '<div class="field"><label for="pref-hour">Daily brief time</label>'
            + _render_hour_select(int(prefs["hour"]))
            + "</div>"
            + '<div class="field"><label for="pref-tz">Timezone (applies to Upcoming &amp; emails)</label>'
            + _render_timezone_select(tz_name)
            + "</div></div></article>"
        )

        news_card = ""
        if news_enabled:
            pause_until = str(prefs.get("news_pause_until") or "")
            if not prefs.get("news_digest"):
                status_badge = '<span class="badge">Off</span>'
            elif pause_until and pause_until >= str(local_today):
                status_badge = (
                    f'<span class="badge warn">Paused until {html.escape(pause_until)}</span>'
                )
            else:
                status_badge = '<span class="badge ok">Active</span>'

            slot_a_local = format_utc_slot(23, 30, tz_name, ref_date=local_today)
            slot_b_local = format_utc_slot(11, 30, tz_name, ref_date=local_today)
            cadence_options = [
                ("twice_daily", f"Twice daily ({slot_a_local} and {slot_b_local})"),
                ("slot_a", f"Once daily · {slot_a_local} (23:30 UTC)"),
                ("slot_b", f"Once daily · {slot_b_local} (11:30 UTC)"),
            ]
            radios_html = "".join(
                '<label class="check"><input type="radio" name="news_cadence" value="'
                + key
                + '"'
                + (" checked" if prefs.get("news_cadence") == key else "")
                + "> "
                + html.escape(label)
                + "</label>"
                for key, label in cadence_options
            )

            scope_html = ""
            if show_holdings:
                current_scope = prefs.get("news_scope", "both")
                scope_opts = "".join(
                    f'<option value="{val}"'
                    + (" selected" if current_scope == val else "")
                    + f">{html.escape(label)}</option>"
                    for val, label in [
                        ("both", "Holdings and watchlist"),
                        ("holdings", "Portfolio holdings only"),
                        ("watchlist", "Watchlist only"),
                    ]
                )
                scope_html = (
                    '<div class="field"><label for="news-scope">Company scope</label>'
                    f'<select id="news-scope" name="news_scope">{scope_opts}</select></div>'
                )

            news_card = (
                '<article class="panel">'
                f'<div class="card-head"><h2>Company news digest</h2>{status_badge}</div>'
                '<p class="muted small">Delivered to '
                + html.escape(email_label)
                + " · times shown in "
                + html.escape(tz_name)
                + ".</p>"
                '<div class="stack">'
                '<label class="check"><input type="checkbox" name="news_digest"'
                + (" checked" if prefs.get("news_digest") else "")
                + "> Enable company news digest</label>"
                + '<div class="field"><label for="news-pause">Pause until (optional)</label>'
                + f'<input id="news-pause" type="date" name="news_pause_until" value="{html.escape(pause_until, quote=True)}"></div>'
                + f'<fieldset class="radios"><legend>Delivery cadence</legend>{radios_html}</fieldset>'
                + '<label class="check"><input type="checkbox" name="news_weekdays_only"'
                + (" checked" if prefs.get("news_weekdays_only") else "")
                + "> Weekdays only</label>"
                + scope_html
                + "</div></article>"
            )

        deliveries_rows = accounts.recent_deliveries(settings.db, profile["person"], limit=20)
        kind_labels = {
            "results": "Results",
            "brief": "Daily brief",
            "weekly_brief": "Weekly brief",
            "urgent": "Urgent",
        }
        state_labels = {
            "sent": ("Sent", "badge ok"),
            "pending": ("Pending", "badge"),
            "unknown": ("Waiting review", "badge warn"),
            "suppressed": ("Suppressed", "badge"),
            "superseded": ("Superseded", "badge"),
        }
        try:
            user_tz = ZoneInfo(tz_name)
        except (ValueError, ZoneInfoNotFoundError):
            user_tz = ZoneInfo("UTC")
        if deliveries_rows:
            d_items = []
            for drow in deliveries_rows:
                k_label = kind_labels.get(drow["kind"], drow["kind"])
                s_label, s_cls = state_labels.get(drow["state"], (drow["state"], "pill"))
                sent_str = "Not sent yet"
                if drow.get("sent_at"):
                    try:
                        dt = datetime.fromisoformat(drow["sent_at"])
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        sent_str = dt.astimezone(user_tz).strftime("%Y-%m-%d %H:%M %Z")
                    except ValueError:
                        sent_str = str(drow["sent_at"])[:19]
                d_items.append(
                    '<div class="company-row">'
                    '<div class="company-main">'
                    '<div class="company-head">'
                    f'<span class="company-name">{html.escape(k_label)}</span>'
                    f'<span class="{s_cls}">{html.escape(s_label)}</span>'
                    "</div>"
                    f'<p class="company-sub"><code>{html.escape(drow["key"])}</code></p>'
                    "</div>"
                    f'<span class="muted small">{html.escape(sent_str)}</span>'
                    "</div>"
                )
            history_html = (
                '<section class="history-section" aria-label="Recent earnings deliveries">'
                '<div class="card-head"><h2>Recent deliveries</h2></div>'
                f'<div class="company-list">{"".join(d_items)}</div></section>'
            )
        else:
            history_html = (
                '<section class="history-section" aria-label="Recent earnings deliveries">'
                '<div class="card-head"><h2>Recent deliveries</h2></div>'
                '<p class="muted small">No notifications recorded yet.</p></section>'
            )

        section_val = "all" if news_enabled else "earnings"
        return (
            '<form method="post" action="/connections/preferences" data-async="reload">'
            + form_fields(profile, view="digests")
            + f'<input type="hidden" name="section" value="{section_val}">'
            + f'<div class="cards-grid">{alerts_card}{news_card}</div>'
            + '<div class="form-error" role="alert"></div>'
            + '<div class="actions-row">'
            + button("Save preferences", cls="button primary")
            + '<a class="button secondary" href="/connections/?view=preview">Preview next brief</a>'
            + "</div></form>"
            + history_html
        )

    def _render_preview_tab(service, profile, *, show_holdings: bool) -> str:
        tz_name = profile["timezone"]
        pending = notify.notices(settings, profile["person"], include_holdings=show_holdings)
        cards: list[str] = []

        for _key, kind, payload in pending:
            rendered = render_email(kind, payload, timezone_name=tz_name)
            cards.append(
                '<article class="panel">'
                f'<div class="card-head"><h2>{html.escape(rendered.subject)}</h2>'
                f'<span class="badge ok">{html.escape(kind)}</span></div>'
                f'<pre class="hub-preview-text">{html.escape(rendered.text.strip())}</pre>'
                "</article>"
            )

        if not cards:
            sample = service.upcoming(days=7, include_holdings=show_holdings)
            sample_events = sample["events"]
            try:
                local_today = datetime.now(ZoneInfo(tz_name)).date()
            except (ValueError, ZoneInfoNotFoundError):
                local_today = datetime.now(timezone.utc).date()
            sample_payload = {
                "day": str(local_today),
                "events": sample_events,
                "changes": [],
                "documents": [],
                "timezone": tz_name,
                "has_holdings": show_holdings,
            }
            rendered = render_email("weekly_brief", sample_payload, timezone_name=tz_name)
            cards.append(
                '<article class="panel">'
                f'<div class="card-head"><h2>{html.escape(rendered.subject)}</h2>'
                '<span class="badge">Sample (none due right now)</span></div>'
                '<p class="muted small">No notification is due at this moment (quiet day or before your brief hour). '
                "Here is what a 7-day brief looks like from your current follows:</p>"
                f'<pre class="hub-preview-text">{html.escape(rendered.text.strip())}</pre>'
                "</article>"
            )

        return (
            '<div class="actions-row preview-back">'
            '<a class="button secondary" href="/connections/?view=digests">← Back to digests</a>'
            "</div>" + "".join(cards)
        )

    def _render_calendar_tab(service, profile, *, is_windows: bool = False) -> str:
        token = accounts.feed_token(settings.db, profile["person"], service.signing_key)
        link = settings.public_url.rstrip("/") + "/calendar/" + token + ".ics"
        has_webcal = link.startswith("https://") and not is_windows
        actions_row = ""
        if has_webcal:
            webcal_link = "webcal://" + link.removeprefix("https://")
            actions_row = (
                '<div class="actions-row">'
                f'<a class="button primary" href="{html.escape(webcal_link, quote=True)}">'
                "Subscribe in calendar app</a>"
                "</div>"
            )
        subscribe_hint = (
            '<div class="callout info">'
            "In Outlook, copy the private link below and paste it into "
            "<strong>Add calendar &rarr; Subscribe from web</strong>.</div>"
            if is_windows
            else ""
        )
        copy_cls = "button secondary btn-sm" if has_webcal else "button primary btn-sm"
        return (
            '<article class="panel">'
            "<h2>Calendar subscription</h2>"
            '<p class="muted">Subscribe to a read-only calendar of your companies. Calendar apps control refresh timing, which can take hours. Your private link grants access to these events; keep it private.</p>'
            f"{actions_row}"
            f"{subscribe_hint}"
            '<div class="copy-box">'
            '<div class="eyebrow">Private calendar link</div>'
            '<div class="copy-row">'
            f'<code id="calendar-link">{html.escape(link, quote=True)}</code>'
            f'<button type="button" class="{copy_cls}" data-copy-code="calendar-link" data-copied="Link copied">Copy link</button>'
            "</div></div>"
            '<div class="calendar-manage">'
            '<button type="button" class="button danger-link btn-sm" popovertarget="reset-feed-popover">Reset private link…</button>'
            "</div>"
            '<div id="reset-feed-popover" popover>'
            "<h2>Reset private calendar link?</h2>"
            '<p class="muted">Existing calendar subscriptions using the current link will stop updating. You will need to subscribe again with the new link.</p>'
            '<div class="actions-row">'
            '<form method="post" action="/connections/reset-feed">'
            + form_fields(profile, view="calendar")
            + button("Reset link", cls="button danger-solid")
            + "</form>"
            '<button type="button" class="button secondary" popovertarget="reset-feed-popover" popovertargetaction="hide">Cancel</button>'
            "</div></div></article>"
        )

    async def static_asset(request):
        name = request.path_params["filename"]
        media_type = _STATIC_ASSETS.get(name)
        if not media_type:
            return Response("Not found.", status_code=404)
        content = resources.files("earnings_watch.web").joinpath(name).read_bytes()
        headers = {"Cache-Control": "public, max-age=31536000, immutable"}
        if not auth:
            headers["X-Content-Type-Options"] = "nosniff"
        return Response(content, media_type=media_type, headers=headers)

    async def home(request):
        try:
            person, info = await identity(request)
            if not person:
                return (
                    RedirectResponse("/connections/login", 303)
                    if auth
                    else Response("Browser sign-in is not configured.", 503)
                )
            profile = accounts.profile(settings.db, person)
            page_seq = Catalogue(settings.db).status()["sequence"]
            seen = profile.get("seen_seq") or 0
            changed = accounts.changed_ids(settings.db, person, seen, page_seq)
            follows = PersonalStore(settings.db, person).follows()
            show_holdings = _is_portfolio_admitted(info, follows)
            service = open_service(settings, person, include_holdings=show_holdings)
            news_enabled = settings.integrations.news_digest.enabled

            view = request.query_params.get("view", "upcoming")
            if view not in _VIEWS:
                view = "upcoming"
            filter_by = request.query_params.get("filter", "all")
            if filter_by not in _FILTERS:
                filter_by = "all"

            titles = {
                "upcoming": ("Upcoming", "Dates and documents for the companies you follow."),
                "companies": (
                    "Companies",
                    "Manage your holdings, watchlist, and alert toggles.",
                ),
                "digests": (
                    "Digests & alerts",
                    "Choose which emails you get, when they arrive, and your timezone.",
                ),
                "calendar": (
                    "Calendar",
                    "Subscribe to a private, read-only feed of your company dates.",
                ),
                "preview": (
                    "Brief preview",
                    "A look at what your next earnings email will contain.",
                ),
            }
            page_title, page_intro = titles[view]

            if view == "upcoming":
                tab_body = _render_upcoming_tab(
                    service, profile, changed, show_holdings=show_holdings, filter_by=filter_by
                )
            elif view == "companies":
                tab_body = _render_companies_tab(
                    service,
                    profile,
                    follows,
                    show_holdings=show_holdings,
                    news_enabled=news_enabled,
                )
            elif view == "digests":
                tab_body = _render_digests_tab(
                    profile, info, show_holdings=show_holdings, news_enabled=news_enabled
                )
            elif view == "preview":
                tab_body = _render_preview_tab(service, profile, show_holdings=show_holdings)
            else:
                tab_body = _render_calendar_tab(
                    service, profile, is_windows=_is_windows_client(request.headers)
                )

            nav_view = "digests" if view == "preview" else view
            account_label = info.get("displayName") or info.get("email") or ""
            coverage_warnings = service.status().get("coverage_warnings", [])
            return _hub_html(
                hub_page(
                    page_title,
                    tab_body,
                    intro=page_intro,
                    active_view=nav_view,
                    account_label=account_label,
                    coverage_warnings=coverage_warnings,
                    suite=_suite_for(request),
                )
            )
        except EarningsError as exc:
            return _hub_html(
                hub_page(
                    "Earnings Watch is unavailable",
                    '<article class="panel"><p>' + html.escape(str(exc)) + "</p></article>",
                    show_nav=False,
                ),
                403,
            )
        except Exception:
            return _hub_html(
                hub_page(
                    "Earnings Watch is temporarily unavailable",
                    '<article class="panel"><p>Your companies and preferences are safe. Try again shortly.</p></article>',
                    show_nav=False,
                ),
                503,
            )

    async def change(request):
        wants_json = _wants_json(request)
        form = None
        try:
            person, info = await identity(request)
            if not person or not auth.origin_ok(request):
                if wants_json:
                    return JSONResponse(
                        {"ok": False, "error": "Sign in and refresh this page before saving."},
                        status_code=403,
                        headers={"Cache-Control": "no-store"},
                    )
                return Response("Sign in and refresh this page before saving.", 403)
            # Stream first, so chunked bodies cannot bypass the bound.
            data = bytearray()
            async for chunk in request.stream():
                data.extend(chunk)
                if len(data) > MAX_BODY:
                    return Response("Import is too large.", 413)
            request._body = bytes(data)
            form = await request.form(max_files=1, max_fields=25, max_part_size=MAX_BODY)
            action = request.path_params["action"]
            if (
                action in ("import-preview", "import-apply")
                and directory is not None
                and info.get("portfolio_admitted") is not True
            ):
                return Response("Holdings import is not available for this account.", 403)
            if not settings.integrations.news_digest.enabled and (
                action in ("news-mute", "news-unmute")
                or (action == "preferences" and str(form.get("section", "")).strip() == "news")
            ):
                return Response("Not found.", 404)
            revision = int(str(form.get("revision", "-1")))
            current_profile = accounts.profile(settings.db, person)
            if revision != current_profile["revision"]:
                raise EarningsError("conflict", "This page changed. Refresh before saving.")
            ticker = str(form.get("ticker", "")).upper().strip()
            exchange = str(form.get("exchange", "")).upper().strip()
            view = str(form.get("view", "")).strip()
            redirect_url = (
                f"/connections/?view={view}&keep_seen=1"
                if view in _VIEWS and view != "upcoming"
                else "/connections/?keep_seen=1"
            )
            json_extra: dict = {}
            if action == "preferences":
                section = str(form.get("section", "")).strip()
                updated = dict(current_profile["settings"])
                tz = current_profile["timezone"]
                if section in ("news", "all") and settings.integrations.news_digest.enabled:
                    follows = PersonalStore(settings.db, person).follows()
                    show_holdings = _is_portfolio_admitted(info, follows)
                    updated["news_digest"] = form.get("news_digest") == "on"
                    updated["news_cadence"] = str(
                        form.get("news_cadence", updated["news_cadence"])
                    ).strip()
                    updated["news_weekdays_only"] = form.get("news_weekdays_only") == "on"
                    updated["news_pause_until"] = str(form.get("news_pause_until", "")).strip()
                    if show_holdings and "news_scope" in form:
                        updated["news_scope"] = str(form.get("news_scope")).strip()
                    elif not show_holdings:
                        updated["news_scope"] = "watchlist"
                if section != "news":
                    for key in ("results", "brief", "weekly_brief", "urgent"):
                        updated[key] = form.get(key) == "on"
                    updated["hour"] = int(str(form.get("hour")))
                    tz = str(form.get("timezone"))
                accounts.preferences(settings.db, person, revision, updated, tz)
                json_extra = {"message": "Preferences saved."}
            elif action == "watch":
                ir_url = str(form.get("ir_url", "")).strip() or None
                company_name = str(form.get("name", "")).strip()[:120] or None
                result = PersonalStore(settings.db, person).watch(
                    str(form.get("ticker", "")).strip(),
                    exchange,
                    name=company_name,
                    ir_url=ir_url,
                    revision=revision,
                )
                if result["state"] == "ambiguous":
                    exchanges = sorted({r["exchange"] for r in result["candidates"]})
                    raise EarningsError(
                        "ambiguous",
                        f"Choose an exchange for {ticker}: {', '.join(exchanges)}. "
                        "Enter its MIC in the Exchange field.",
                    )
                json_extra = {"message": f"Added {ticker} to watchlist."}
            elif action == "mark-seen":
                current_seq = Catalogue(settings.db).status()["sequence"]
                raw_seq = str(form.get("seen_seq", "")).strip()
                target_seq = min(int(raw_seq), current_seq) if raw_seq else current_seq
                accounts.mark_seen(settings.db, person, max(0, target_seq))
                if wants_json:
                    latest_rev = accounts.profile(settings.db, person)["revision"]
                    return JSONResponse(
                        {"ok": True, "revision": latest_rev, "message": "Changes marked as seen."},
                        headers={"Cache-Control": "no-store"},
                    )
                return RedirectResponse("/connections/", 303)
            elif action == "unwatch":
                PersonalStore(settings.db, person).unwatch(
                    ticker,
                    exchange,
                    revision=revision,
                    include_holdings=(
                        directory is None or bool(info.get("portfolio_admitted") is True)
                    ),
                )
                json_extra = {"message": f"Removed {ticker} from watchlist."}
            elif action in ("mute", "unmute"):
                is_muting = action == "mute"
                accounts.mute(settings.db, person, ticker, exchange, is_muting, revision=revision)
                remaining = accounts.mutes(settings.db, person)
                wildcard_active = (ticker, "") in remaining
                if exchange == "" or wildcard_active:
                    json_extra = {
                        "channel": "calendar",
                        "muted": wildcard_active or (ticker, exchange) in remaining,
                        "reload": True,
                        "message": (
                            f"Muted calendar alerts for {ticker}."
                            if is_muting
                            else f"Updated calendar alerts for {ticker}."
                        ),
                    }
                else:
                    json_extra = {
                        "channel": "calendar",
                        "muted": is_muting,
                        "next_action": "unmute" if is_muting else "mute",
                        "next_exchange": exchange,
                        "next_label": "Calendar: Off" if is_muting else "Calendar: On",
                        "message": (
                            f"Muted calendar alerts for {ticker}."
                            if is_muting
                            else f"Enabled calendar alerts for {ticker}."
                        ),
                    }
            elif action in ("news-mute", "news-unmute"):
                is_muting = action == "news-mute"
                accounts.news_mute(
                    settings.db, person, ticker, exchange, is_muting, revision=revision
                )
                remaining_news = set(
                    accounts.profile(settings.db, person)["settings"]["news_mutes"]
                )
                wildcard_news = f"{ticker}:" in remaining_news
                if exchange == "" or wildcard_news:
                    json_extra = {
                        "channel": "news",
                        "muted": wildcard_news or f"{ticker}:{exchange}" in remaining_news,
                        "reload": True,
                        "message": (
                            f"Muted news digest for {ticker}."
                            if is_muting
                            else f"Updated news digest for {ticker}."
                        ),
                    }
                else:
                    json_extra = {
                        "channel": "news",
                        "muted": is_muting,
                        "next_action": "news-unmute" if is_muting else "news-mute",
                        "next_exchange": exchange,
                        "next_label": "News: Off" if is_muting else "News: On",
                        "message": (
                            f"Muted news digest for {ticker}."
                            if is_muting
                            else f"Enabled news digest for {ticker}."
                        ),
                    }
            elif action == "reset-feed":
                accounts.feed_token(
                    settings.db,
                    person,
                    (settings.data_dir / "calendar.key").read_bytes(),
                    rotate=True,
                    revision=revision,
                )
                json_extra = {"message": "Private calendar link reset."}
            elif action == "import-preview":
                text = str(form.get("companies", ""))
                upload = form.get("file")
                if isinstance(upload, UploadFile) and upload.filename:
                    try:
                        text = (await upload.read(MAX_BODY)).decode("utf-8-sig")
                    except UnicodeDecodeError:
                        raise EarningsError(
                            "invalid_argument", "Upload a UTF-8 CSV or plain text file."
                        ) from None
                result = interests.preview(settings, person, text)
                unlisted_html = ""
                if result.get("unlisted"):
                    unlisted_html = (
                        "<p>Will queue "
                        + str(len(result["unlisted"]))
                        + " unlisted companies for automatic registration:</p><ul>"
                        + "".join(
                            "<li>"
                            + html.escape(
                                (r["ticker"] + (" " + r["exchange"] if r["exchange"] else ""))
                            )
                            + "</li>"
                            for r in result["unlisted"]
                        )
                        + "</ul>"
                    )
                body = (
                    '<article class="panel"><p>Adds '
                    + str(result["adds"])
                    + ", removes "
                    + str(result["removes"])
                    + " uploaded holdings. Mutes, portfolio sync and watchlist are preserved.</p><ul>"
                    + "".join(
                        "<li>" + html.escape(r["ticker"] + " " + r["exchange"]) + "</li>"
                        for r in result["items"]
                    )
                    + "</ul>"
                    + unlisted_html
                    + '<div class="actions-row"><form method="post" action="/connections/import-apply">'
                    + form_fields(accounts.profile(settings.db, person), view="companies")
                    + '<input type="hidden" name="handle" value="'
                    + html.escape(result["handle"], quote=True)
                    + '">'
                    + button("Replace uploaded holdings", cls="button primary")
                    + '</form><a class="button secondary" href="/connections/?view=companies">Cancel</a></div></article>'
                )
                account_label = info.get("displayName") or info.get("email") or ""
                return _hub_html(
                    hub_page(
                        "Review holdings import",
                        body,
                        intro="Check what will change before replacing your uploaded holdings.",
                        active_view="companies",
                        account_label=account_label,
                        suite=_suite_for(request),
                    )
                )
            elif action == "import-apply":
                interests.apply(settings, person, str(form.get("handle", "")))
                json_extra = {"message": "Uploaded holdings replaced."}
            else:
                return Response("Unknown action.", 404)
            if wants_json:
                latest_rev = accounts.profile(settings.db, person)["revision"]
                return JSONResponse(
                    {"ok": True, "revision": latest_rev, **json_extra},
                    headers={"Cache-Control": "no-store"},
                )
            return RedirectResponse(redirect_url, 303)
        except (EarningsError, ValueError, KeyError) as exc:
            if isinstance(exc, EarningsError) and exc.code == "unauthorized":
                if wants_json:
                    return JSONResponse(
                        {"ok": False, "error": str(exc)},
                        status_code=403,
                        headers={"Cache-Control": "no-store"},
                    )
                return _hub_html(
                    hub_page(
                        "Access unavailable",
                        '<article class="panel"><p>' + html.escape(str(exc)) + "</p></article>",
                        show_nav=False,
                    ),
                    403,
                )
            if wants_json:
                return JSONResponse(
                    {"ok": False, "error": str(exc)},
                    status_code=409,
                    headers={"Cache-Control": "no-store"},
                )
            back_view = ""
            if form is not None:
                try:
                    back_view = str(form.get("view", "")).strip()
                except Exception:
                    back_view = ""
            back_href = (
                f"/connections/?view={back_view}"
                if back_view in _VIEWS and back_view != "upcoming"
                else "/connections/"
            )
            return _hub_html(
                hub_page(
                    "Please review your change",
                    '<article class="panel"><p>'
                    + html.escape(str(exc))
                    + f'</p><div class="actions-row"><a class="button primary" href="{back_href}">Return to Earnings Watch</a></div></article>',
                    active_view=back_view if back_view in _VIEWS else "upcoming",
                    show_nav=False,
                ),
                409,
            )
        except Exception:
            if wants_json:
                return JSONResponse(
                    {
                        "ok": False,
                        "error": "Could not save right now. Refresh the page and try again.",
                    },
                    status_code=503,
                    headers={"Cache-Control": "no-store"},
                )
            return _hub_html(
                hub_page(
                    "Could not save right now",
                    '<article class="panel"><p>Refresh the page and try again. Your existing preferences are retained.</p></article>',
                    show_nav=False,
                ),
                503,
            )

    async def internal_targets(request):
        if not _valid_bearer(dict(request.headers), settings.targets_tokens_env):
            return JSONResponse(
                {"error": "unauthorized"}, status_code=401, headers={"Cache-Control": "no-store"}
            )
        if settings.directory_required and directory is None:
            return JSONResponse(
                {"error": "identity directory unavailable"},
                status_code=503,
                headers={"Cache-Control": "no-store"},
            )
        try:
            payload = await _targets_payload(settings, directory)
        except Exception:
            return JSONResponse(
                {"error": "identity directory unavailable"},
                status_code=503,
                headers={"Cache-Control": "no-store"},
            )
        return JSONResponse(payload, status_code=200, headers={"Cache-Control": "no-store"})

    async def internal_library_targets(request):
        if not _valid_bearer(dict(request.headers), settings.library_targets_tokens_env):
            return JSONResponse(
                {"error": "unauthorized"}, status_code=401, headers={"Cache-Control": "no-store"}
            )
        if settings.directory_required and directory is None:
            return JSONResponse(
                {"error": "identity directory unavailable"},
                status_code=503,
                headers={"Cache-Control": "no-store"},
            )
        raw_schema = request.query_params.get("schema", "1").strip()
        if raw_schema not in {"1", "2"}:
            return JSONResponse(
                {"error": "unsupported schema version"},
                status_code=400,
                headers={"Cache-Control": "no-store"},
            )
        try:
            payload = await _library_targets_payload(
                settings, directory, schema_version=int(raw_schema)
            )
        except Exception:
            return JSONResponse(
                {"error": "identity directory unavailable"},
                status_code=503,
                headers={"Cache-Control": "no-store"},
            )
        return JSONResponse(payload, status_code=200, headers={"Cache-Control": "no-store"})

    async def calendar_feed(request):
        person = await asyncio.to_thread(
            accounts.feed_person, settings.db, request.path_params["token"]
        )
        if not person:
            return Response(status_code=404)
        try:
            include_holdings = True
            if directory:
                who = await directory.resolve(person)
                if not who["admitted"] or who["disabled"]:
                    return Response(status_code=404)
                include_holdings = bool(who.get("portfolio_admitted") is True)
            elif settings.directory_required:
                return Response(status_code=503)
            content = await asyncio.to_thread(
                lambda: open_service(settings, person).calendar_ics(
                    include_holdings=include_holdings
                )
            )
            return Response(
                content,
                media_type="text/calendar; charset=utf-8",
                headers={
                    "Cache-Control": "private, no-store",
                    "Content-Disposition": 'inline; filename="earnings-watch.ics"',
                    "Referrer-Policy": "no-referrer",
                    "X-Content-Type-Options": "nosniff",
                },
            )
        except Exception:
            return Response(status_code=503)

    async def health(request):
        try:
            result = await asyncio.to_thread(integrity, settings.db)
            return JSONResponse({"integrity": result}, 200 if result == "ok" else 503)
        except Exception:
            return JSONResponse({"error": "unavailable"}, 503)

    @asynccontextmanager
    async def lifespan(app):
        async with native.app.router.lifespan_context(native.app):
            yield

    routes: list[BaseRoute] = [
        Route("/", lambda request: RedirectResponse("/connections/", 303)),
        Route("/health", health),
        Route("/internal/targets", internal_targets, methods=["GET"]),
        Route("/internal/library-targets", internal_library_targets, methods=["GET"]),
        Route("/connections/assets/{filename}", static_asset, methods=["GET"]),
        Route("/connections/", home),
        Route("/calendar/{token}.ics", calendar_feed),
    ]
    if auth:
        routes += auth.routes()
    routes += [Route("/connections/{action}", change, methods=["POST"]), Mount("/", app=native)]
    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.earnings_auth = auth
    app.state.earnings_native = native
    wrapped = auth.middleware(app) if auth else app

    # Deduplicate headers appended by BrowserAuth.middleware so app-owned Referrer-Policy,
    # CSP, and immutable static-asset Cache-Control take precedence cleanly.
    async def normalize_headers(scope, receive, send):
        if scope["type"] != "http":
            return await wrapped(scope, receive, send)
        path = scope.get("path", "")
        is_asset = path.startswith("/connections/assets/")

        async def send_clean(message):
            if message["type"] == "http.response.start" and path.startswith("/connections/"):
                seen: set[bytes] = set()
                cleaned: list[tuple[bytes, bytes]] = []
                for key, val in message.get("headers", []):
                    lower = key.lower()
                    if lower == b"set-cookie":
                        cleaned.append((key, val))
                        continue
                    if is_asset and lower == b"content-security-policy":
                        continue
                    if lower in seen:
                        continue
                    seen.add(lower)
                    cleaned.append((key, val))
                message = {**message, "headers": cleaned}
            await send(message)

        return await wrapped(scope, receive, send_clean)

    # All surfaces, including the bearer calendar URL and internal targets API, share one Host boundary.
    from starlette.middleware.trustedhost import TrustedHostMiddleware

    outer = TrustedHostMiddleware(normalize_headers, allowed_hosts=settings.allowed_hosts)
    setattr(outer, "earnings_app", app)
    return outer
