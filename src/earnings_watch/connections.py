"""Connections UI, private calendar subscriptions, and read-only internal targets API."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
import hmac
import html
import json
import os
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from signin_window.oidc import BrowserAuth, OIDCConfig
from starlette.applications import Starlette
from starlette.datastructures import UploadFile
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import BaseRoute, Mount, Route

from . import interests, notify
from .composition import open_service
from .errors import EarningsError
from .http_server import AuthenticatedApp
from .presentation import format_utc_slot, group_upcoming
from .render import render_email
from .store import accounts
from .store.database import Catalogue, LibraryReader, PersonalStore, integrity, people
from .transport import IdentityDirectory
from .ui import page, renderer_options

MAX_BODY = 65536
_VIEWS = frozenset({"upcoming", "companies", "digests", "calendar", "preview"})
_FILTERS = frozenset({"all", "holdings", "watchlist", "earnings"})
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


_HUB_STYLE = """
<style>
main{max-width:1080px}
.hub-topbar{display:flex;justify-content:space-between;align-items:center;gap:1rem;flex-wrap:wrap;margin-bottom:1.25rem;border-bottom:1px solid var(--line);padding-bottom:.75rem}
.hub-tabs{display:flex;gap:.4rem;flex-wrap:wrap}
.hub-tab{display:inline-flex;align-items:center;min-height:40px;padding:.45rem .9rem;border-radius:.6rem;text-decoration:none;color:var(--muted);font-weight:600;border:1px solid transparent}
.hub-tab:hover{color:var(--text);background:var(--surface)}
.hub-tab[aria-current="page"]{color:var(--on-accent);background:var(--accent);border-color:var(--accent)}
.hub-pills{display:flex;gap:.4rem;flex-wrap:wrap;margin:.75rem 0 1rem}
.hub-pill{display:inline-flex;align-items:center;min-height:34px;padding:.25rem .75rem;border-radius:999px;border:1px solid var(--line);text-decoration:none;color:var(--text);background:var(--surface);font-size:.9rem;font-weight:500}
.hub-pill[aria-current="true"]{background:var(--accent);color:var(--on-accent);border-color:var(--accent)}
.hub-section{margin:1.25rem 0}
.hub-group-title{font-size:.85rem;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin:1.2rem 0 .5rem}
.hub-events{list-style:none;padding:0;margin:0;display:grid;gap:.65rem}
.hub-event{background:var(--surface);border:1px solid var(--line);border-radius:.75rem;padding:.9rem 1.1rem;display:flex;justify-content:space-between;align-items:flex-start;gap:1rem;flex-wrap:wrap}
.hub-event-main{flex:1 1 340px;min-width:0}
.hub-event-meta{display:flex;gap:.45rem;flex-wrap:wrap;align-items:center;margin-top:.3rem;font-size:.9rem;color:var(--muted)}
.hub-event-links{display:flex;gap:.6rem;flex-wrap:wrap;align-items:center;font-size:.9rem}
.hub-badge{display:inline-flex;align-items:center;padding:.15rem .55rem;border-radius:999px;font-size:.78rem;font-weight:600;background:var(--status-bg);color:var(--text);border:1px solid var(--line)}
.hub-badge-confirmed{border-color:var(--accent);color:var(--accent)}
.hub-badge-changed{background:var(--accent);color:var(--on-accent);border-color:var(--accent)}
.hub-badge-paused{border-style:dashed}
.hub-preview-text{white-space:pre-wrap;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.88rem;line-height:1.5;background:var(--bg);border:1px solid var(--line);border-radius:.65rem;padding:.9rem 1rem;margin:.75rem 0 0;overflow-x:auto}
.hub-badge-off{opacity:.75}
.hub-toolbar{display:flex;gap:.75rem;flex-wrap:wrap;align-items:flex-end;background:var(--surface);border:1px solid var(--line);border-radius:.85rem;padding:1rem;margin-bottom:1.25rem}
.hub-toolbar form{display:flex;gap:.65rem;flex-wrap:wrap;align-items:flex-end;margin:0;flex:1 1 480px}
.hub-field{display:flex;flex-direction:column;gap:.25rem;min-width:160px;flex:1 1 180px}
.hub-field input,.hub-field select,.hub-field textarea{padding:.5rem .65rem;border:1px solid var(--line);border-radius:.5rem;background:var(--bg);color:var(--text);font:inherit;min-height:42px}
.hub-table-wrap{overflow-x:auto;background:var(--surface);border:1px solid var(--line);border-radius:.85rem;margin:.75rem 0 1.25rem}
.hub-table{width:100%;border-collapse:collapse;font-size:.93rem}
.hub-table th,.hub-table td{padding:.65rem .85rem;text-align:left;border-bottom:1px solid var(--line);vertical-align:middle}
.hub-table tr:last-child td{border-bottom:none}
.hub-table th{font-size:.78rem;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);font-weight:700}
.hub-inline-form{display:inline-block;margin:0}
.hub-toggle{min-height:32px;padding:.25rem .65rem;font-size:.82rem;border-radius:999px}
.hub-cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:1rem;align-items:start}
.hub-card{background:var(--surface);border:1px solid var(--line);border-radius:.9rem;padding:1.25rem}
.hub-card-head{display:flex;justify-content:space-between;align-items:center;gap:.75rem;margin-bottom:.75rem}
.hub-card-head h2{margin:0}
.hub-stack{display:flex;flex-direction:column;gap:.75rem;width:100%}
.hub-stack form{display:flex;flex-direction:column;gap:.75rem;margin:0;width:100%}
.hub-check{display:flex;align-items:center;gap:.45rem;cursor:pointer}
.hub-radios{display:flex;flex-direction:column;gap:.45rem;border:1px solid var(--line);border-radius:.6rem;padding:.75rem;margin:0}
.hub-radios legend{font-size:.85rem;font-weight:600;color:var(--muted);padding:0 .3rem}
.hub-code-box{display:flex;gap:.65rem;flex-wrap:wrap;align-items:center;padding:.75rem;border:1px solid var(--line);border-radius:.6rem;background:var(--bg);margin:.75rem 0}
.hub-code-box code{flex:1 1 280px;font-size:.9rem}
.hub-actions{display:flex;gap:.65rem;flex-wrap:wrap;align-items:center;margin-top:.75rem}
[popover]{background:var(--surface);color:var(--text);border:1px solid var(--line);border-radius:.9rem;padding:1.4rem;max-width:520px;width:calc(100vw - 2rem);box-shadow:0 16px 40px rgba(0,0,0,.35)}
[popover]::backdrop{background:rgba(0,0,0,.55)}
.hub-signout{margin:0}
.hub-signout button{min-height:36px;padding:.35rem .8rem;font-size:.88rem}
</style>
""".strip()


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
    for person_row in people(settings.db):
        subject = person_row["id"]
        if directory is not None:
            info = await directory.resolve(subject)
            if not info["admitted"] or info["disabled"]:
                continue
            email = str(info.get("email") or "")
            portfolio_admitted = bool(info.get("portfolio_admitted") is True)
        else:
            email = accounts.remembered_email(settings.db, subject)
            portfolio_admitted = False
        prof = accounts.profile(settings.db, subject)
        prefs = prof["settings"]
        follows = PersonalStore(settings.db, subject).follows()
        muted = accounts.mutes(settings.db, subject)
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


async def _library_targets_payload(settings, directory) -> dict:
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
        docs_by_source.setdefault(source_key, []).append(
            {
                "id": str(doc.get("id") or ""),
                "event_id": str(doc.get("event_id") or ""),
                "url": str(doc.get("url") or ""),
                "title": str(doc.get("title") or ""),
                "role": str(doc.get("role") or ""),
                "first_seen": str(doc.get("first_seen") or ""),
            }
        )

    active_source_keys: set[str] = set()
    scopes: list[dict] = []
    for person_row in people(settings.db):
        subject = person_row["id"]
        if directory is not None:
            info = await directory.resolve(subject)
            if not info["admitted"] or info["disabled"]:
                continue
            portfolio_admitted = bool(info.get("portfolio_admitted") is True)
        else:
            portfolio_admitted = False
        follows = PersonalStore(settings.db, subject).follows()
        person_keys: set[str] = set()
        for row in follows:
            if row["origin"].startswith("holdings:") and not portfolio_admitted:
                continue
            source_key = row.get("source_key")
            if source_key and source_key in sources_by_key:
                person_keys.add(source_key)
                active_source_keys.add(source_key)
        scopes.append({"person": subject, "source_keys": sorted(person_keys)})

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

    def button(label: str, *, cls: str = "") -> str:
        class_attr = f' class="{cls}"' if cls else ""
        return f'<button type="submit"{class_attr}>{html.escape(label)}</button>'

    def _certainty_badge(event_row: dict) -> str:
        status = event_row.get("status", "")
        cls = "hub-badge hub-badge-confirmed" if status == "ir-confirmed" else "hub-badge"
        return f'<span class="{cls}">{html.escape(str(event_row.get("certainty") or ""))}</span>'

    def _render_event_card(event_row: dict, changed: set[str], *, show_holdings: bool) -> str:
        doc_links = "".join(
            '<a href="'
            + html.escape(d["url"], quote=True)
            + '" rel="noreferrer">'
            + html.escape(d.get("title") or d["role"])
            + "</a>"
            for d in event_row.get("documents", [])
        )
        changed_html = (
            '<span class="hub-badge hub-badge-changed">Changed</span>'
            if event_row["id"] in changed
            else ""
        )
        company_name = event_row.get("company_name") or event_row["ticker"]
        why = [w for w in event_row.get("why_followed", []) if show_holdings or w != "holding"]
        origin_badges = "".join(
            f'<span class="hub-badge">{html.escape("Holding" if w == "holding" else "Watchlist")}</span>'
            for w in why
        )
        return (
            '<li class="hub-event"><div class="hub-event-main">'
            f"<strong>{html.escape(company_name)}</strong> "
            f'<span class="muted">{html.escape(event_row["title"])}</span> '
            f"{changed_html}"
            '<div class="hub-event-meta">'
            f"<span>{html.escape(event_row['when'])}</span>"
            f"{_certainty_badge(event_row)}"
            f"{origin_badges}"
            "</div></div>"
            '<div class="hub-event-links">'
            f'<a href="{html.escape(event_row["source_url"], quote=True)}" rel="noreferrer">Source</a>'
            f"{doc_links}"
            "</div></li>"
        )

    def _render_tabs(active_view: str) -> str:
        tabs = [
            ("upcoming", "Upcoming"),
            ("companies", "Companies"),
            ("digests", "Digests & Alerts"),
            ("calendar", "Calendar"),
        ]
        links = "".join(
            f'<a class="hub-tab" href="/connections/?view={key}"'
            + (' aria-current="page"' if key == active_view else "")
            + f">{html.escape(label)}</a>"
            for key, label in tabs
        )
        return (
            '<div class="hub-topbar">'
            f'<nav class="hub-tabs" aria-label="Page sections">{links}</nav>'
            '<form class="hub-signout" method="post" action="/connections/logout">'
            + button("Sign out", cls="secondary")
            + "</form></div>"
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
            parts.append('<p role="status">' + html.escape(note) + "</p>")

        pills = [("all", "All")]
        if show_holdings:
            pills.append(("holdings", "Holdings"))
        pills.extend([("watchlist", "Watchlist"), ("earnings", "Earnings")])
        pill_html = "".join(
            '<a class="hub-pill" href="/connections/?view=upcoming'
            + (f"&filter={key}" if key != "all" else "")
            + '"'
            + (' aria-current="true"' if key == filter_by else "")
            + f">{html.escape(label)}</a>"
            for key, label in pills
        )
        header_actions = ""
        if visible_changed:
            header_actions = (
                '<form class="hub-inline-form" method="post" action="/connections/mark-seen">'
                + form_fields(profile, view="upcoming")
                + button("Mark changes seen", cls="secondary hub-toggle")
                + "</form>"
            )
        parts.append(
            f'<div class="hub-pills" role="navigation" aria-label="Filter events">{pill_html}{header_actions}</div>'
        )
        parts.append(f'<p class="muted">Next 90 days · {html.escape(profile["timezone"])}</p>')

        group_defs = [
            ("this_week", "This Week"),
            ("next_week", "Next Week"),
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
                f'<section class="hub-section"><h2 class="hub-group-title">{bucket_label} ({len(bucket)})</h2>'
                f'<ul class="hub-events">{cards}</ul></section>'
            )
        if not any_upcoming:
            parts.append(
                '<p>No upcoming events match this view. <a href="/connections/?view=companies">Add a company</a> to get started.</p>'
            )

        if recent_events:
            recent_cards = "".join(
                _render_event_card(e, visible_changed, show_holdings=show_holdings)
                for e in recent_events
            )
            parts.append(
                f'<details class="hub-section"><summary>Recent Releases (14d) · {len(recent_events)}</summary>'
                f'<ul class="hub-events">{recent_cards}</ul></details>'
            )
        else:
            parts.append(
                '<details class="hub-section"><summary>Recent Releases (14d) · 0</summary>'
                "<p>No releases in the last 14 days.</p></details>"
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
            '<button type="button" class="secondary" popovertarget="import-csv-popover">Import CSV</button>'
            if allow_import
            else ""
        )
        import_popover = (
            '<div id="import-csv-popover" popover>'
            "<h2>Import companies from CSV</h2>"
            '<p class="muted">Paste tickers or MIC:TICKER, or upload a CSV with ticker and exchange columns. Quantities and values are discarded. Review changes before replacing uploaded holdings.</p>'
            '<form class="hub-stack" method="post" action="/connections/import-preview" enctype="multipart/form-data">'
            + form_fields(profile, view="companies")
            + '<div class="hub-field"><label for="import-companies">Companies</label>'
            '<textarea id="import-companies" name="companies" rows="4" maxlength="32768"></textarea></div>'
            '<div class="hub-field"><label for="import-file">CSV file</label>'
            '<input id="import-file" name="file" type="file" accept=".csv,text/csv"></div>'
            '<div class="hub-actions">'
            + button("Preview import")
            + '<button type="button" class="secondary" popovertarget="import-csv-popover" popovertargetaction="hide">Cancel</button>'
            "</div></form></div>"
            if allow_import
            else ""
        )
        request_company_popover = (
            '<div id="request-company-popover" popover>'
            "<h2>Track a new company</h2>"
            '<p class="muted">Enter any stock ticker or ISIN to start tracking a company not yet in the catalogue. Automatic discovery will find its official investor-relations calendar.</p>'
            '<form class="hub-stack" method="post" action="/connections/watch">'
            + form_fields(profile, view="companies")
            + '<div class="hub-field"><label for="req-ticker">Ticker or ISIN</label>'
            '<input id="req-ticker" name="ticker" required maxlength="100" autocomplete="off" placeholder="e.g. NVDA, 7203.T, or US67066G1040"></div>'
            '<div class="hub-field"><label for="req-exchange">Exchange or MIC (optional)</label>'
            '<input id="req-exchange" name="exchange" maxlength="12" autocomplete="off" placeholder="e.g. XNAS, NYSE, XHKG"></div>'
            '<div class="hub-field"><label for="req-name">Company name (optional)</label>'
            '<input id="req-name" name="name" maxlength="120" autocomplete="off" placeholder="e.g. NVIDIA Corporation"></div>'
            '<div class="hub-field"><label for="req-ir-url">Official IR URL hint (optional)</label>'
            '<input id="req-ir-url" name="ir_url" type="url" maxlength="500" autocomplete="off" placeholder="https://investor.example.com/events"></div>'
            '<div class="hub-actions">'
            + button("Queue company for tracking")
            + '<button type="button" class="secondary" popovertarget="request-company-popover" popovertargetaction="hide">Cancel</button>'
            "</div></form></div>"
        )
        quick_add = (
            '<div class="hub-toolbar">'
            '<form method="post" action="/connections/watch">'
            + form_fields(profile, view="companies")
            + '<div class="hub-field"><label for="watch-ticker">Company ticker, name, or ISIN</label>'
            '<input id="watch-ticker" name="ticker" list="companies" required maxlength="100" autocomplete="off" placeholder="e.g. NVDA, 7203.T, or company name">'
            f'<datalist id="companies">{options}</datalist></div>'
            '<div class="hub-field" style="max-width:160px"><label for="watch-exchange">Exchange / MIC</label>'
            '<input id="watch-exchange" name="exchange" maxlength="12" placeholder="Optional"></div>'
            + button("Add to watchlist")
            + "</form>"
            '<button type="button" class="secondary" popovertarget="request-company-popover">Request new company</button>'
            + import_button
            + "</div>"
            '<p class="muted" style="margin-top:-.75rem;margin-bottom:1.25rem">Not tracked yet? Type any ticker (e.g. <code>NVDA</code>, <code>7203.T</code>, <code>NASDAQ:NVDA</code>) or ISIN above, or use <strong>Request new company</strong> to include a company name or official IR URL.</p>'
            + request_company_popover
            + import_popover
        )

        def _toggle_forms(row: dict, *, include_remove: bool) -> tuple[str, str, str]:
            ticker = row["ticker"]
            exchange = row["exchange"]
            exact = (ticker, exchange) in muted
            every = (ticker, "") in muted
            is_muted = exact or every
            target = "" if every and not exact else exchange
            cal_fields = (
                form_fields(profile, view="companies")
                + f'<input type="hidden" name="ticker" value="{html.escape(ticker, quote=True)}">'
                + f'<input type="hidden" name="exchange" value="{html.escape(target if is_muted else exchange, quote=True)}">'
            )
            cal_action = "unmute" if is_muted else "mute"
            cal_label = (
                "Unmute all exchanges"
                if (is_muted and every and not exact)
                else ("Off" if is_muted else "On")
            )
            cal_cls = "secondary hub-toggle" if is_muted else "hub-toggle"
            cal_html = (
                f'<form class="hub-inline-form" method="post" action="/connections/{cal_action}">'
                + cal_fields
                + button(cal_label, cls=cal_cls)
                + "</form>"
            )

            news_html = ""
            if news_enabled:
                news_exact = f"{ticker}:{exchange}" in news_mutes
                news_every = f"{ticker}:" in news_mutes
                is_news_muted = news_exact or news_every
                news_target = "" if news_every and not news_exact else exchange
                news_action = "news-unmute" if is_news_muted else "news-mute"
                news_label = (
                    "Unmute all exchanges"
                    if (is_news_muted and news_every and not news_exact)
                    else ("Off" if is_news_muted else "On")
                )
                news_fields = (
                    form_fields(profile, view="companies")
                    + f'<input type="hidden" name="ticker" value="{html.escape(ticker, quote=True)}">'
                    + f'<input type="hidden" name="exchange" value="{html.escape(news_target if is_news_muted else exchange, quote=True)}">'
                )
                news_cls = "secondary hub-toggle" if is_news_muted else "hub-toggle"
                news_html = (
                    f'<form class="hub-inline-form" method="post" action="/connections/{news_action}">'
                    + news_fields
                    + button(news_label, cls=news_cls)
                    + "</form>"
                )

            remove_html = ""
            if include_remove:
                rm_fields = (
                    form_fields(profile, view="companies")
                    + f'<input type="hidden" name="ticker" value="{html.escape(ticker, quote=True)}">'
                    + f'<input type="hidden" name="exchange" value="{html.escape(exchange, quote=True)}">'
                )
                remove_html = (
                    '<form class="hub-inline-form" method="post" action="/connections/unwatch">'
                    + rm_fields
                    + button("Remove", cls="quiet hub-toggle")
                    + "</form>"
                )
            return cal_html, news_html, remove_html

        def _next_event_cell(source_key: str | None) -> str:
            ev = upcoming_by_source.get(source_key) if source_key else None
            if not ev:
                return '<span class="muted">—</span>'
            return html.escape(f"{ev['when']} ({ev['period']})")

        sections = [quick_add]

        if show_holdings:
            holding_rows = [r for r in follows if r["origin"].startswith("holdings:")]
            news_th = "<th>News</th>" if news_enabled else ""
            if holding_rows:
                trs = []
                for row in holding_rows:
                    name = row.get("company_name") or row["ticker"]
                    feed = row["origin"].removeprefix("holdings:")
                    synced = profile["holdings_sync"].get(feed)
                    sub = f"Synced {synced[:10]}" if feed != "upload" and synced else "Uploaded"
                    cal_html, news_html, _ = _toggle_forms(row, include_remove=False)
                    news_td = f"<td>{news_html}</td>" if news_enabled else ""
                    mic = (
                        f' <span class="muted">{html.escape(row["exchange"])}</span>'
                        if row["exchange"]
                        else ""
                    )
                    trs.append(
                        "<tr>"
                        f'<td><strong>{html.escape(name)}</strong><div class="muted">{html.escape(sub)}</div></td>'
                        f"<td><strong>{html.escape(row['ticker'])}</strong>{mic}</td>"
                        f"<td>{_next_event_cell(row.get('source_key'))}</td>"
                        f"<td>{cal_html}</td>"
                        f"{news_td}"
                        "</tr>"
                    )
                holdings_table = (
                    '<div class="hub-table-wrap"><table class="hub-table">'
                    f"<thead><tr><th>Company</th><th>Ticker / MIC</th><th>Next Event</th><th>Calendar</th>{news_th}</tr></thead>"
                    f"<tbody>{''.join(trs)}</tbody></table></div>"
                )
            else:
                holdings_table = '<p class="muted">No portfolio holdings synced yet.</p>'
            sections.append(
                f'<section class="hub-section"><h2>Portfolio Holdings</h2>{holdings_table}</section>'
            )

        watchlist_rows = [r for r in follows if not r["origin"].startswith("holdings:")]
        news_th = "<th>News</th>" if news_enabled else ""
        if watchlist_rows:
            trs = []
            for row in watchlist_rows:
                name = row.get("company_name") or row["ticker"]
                if row["state"] == "queued":
                    reason = row.get("reason") or ""
                    status_html = '<span class="hub-badge">Discovering...</span>' + (
                        f'<div class="muted">{html.escape(reason)}</div>' if reason else ""
                    )
                else:
                    status_html = '<span class="hub-badge hub-badge-confirmed">Active</span>'
                cal_html, news_html, remove_html = _toggle_forms(row, include_remove=True)
                news_td = f"<td>{news_html}</td>" if news_enabled else ""
                mic = (
                    f' <span class="muted">{html.escape(row["exchange"])}</span>'
                    if row["exchange"]
                    else ""
                )
                trs.append(
                    "<tr>"
                    f"<td><strong>{html.escape(name)}</strong></td>"
                    f"<td><strong>{html.escape(row['ticker'])}</strong>{mic}</td>"
                    f"<td>{status_html}</td>"
                    f"<td>{_next_event_cell(row.get('source_key'))}</td>"
                    f"<td>{cal_html}</td>"
                    f"{news_td}"
                    f"<td>{remove_html}</td>"
                    "</tr>"
                )
            watchlist_table = (
                '<div class="hub-table-wrap"><table class="hub-table">'
                f"<thead><tr><th>Company</th><th>Ticker / MIC</th><th>Status</th><th>Next Event</th><th>Calendar</th>{news_th}<th></th></tr></thead>"
                f"<tbody>{''.join(trs)}</tbody></table></div>"
            )
        else:
            watchlist_table = '<p class="muted">Your watchlist is empty. Add a ticker above to start tracking.</p>'
        sections.append(
            f'<section class="hub-section"><h2>My Watchlist</h2>{watchlist_table}</section>'
        )
        return "".join(sections)

    def _render_digests_tab(profile, info: dict, *, show_holdings: bool, news_enabled: bool) -> str:
        prefs = profile["settings"]
        tz_name = profile["timezone"]
        try:
            local_today = datetime.now(ZoneInfo(tz_name)).date()
        except (ValueError, ZoneInfoNotFoundError):
            local_today = datetime.now(timezone.utc).date()
        email_label = info.get("email") or "No email is available from your identity provider"

        alert_toggles = "".join(
            '<label class="hub-check"><input type="checkbox" name="'
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
            '<article class="hub-card">'
            '<div class="hub-card-head"><h2>Earnings Alerts</h2></div>'
            '<p class="muted">Sent to ' + html.escape(email_label) + " (your sign-in email).</p>"
            '<div class="hub-stack"><form method="post" action="/connections/preferences">'
            + form_fields(profile, view="digests")
            + '<input type="hidden" name="section" value="earnings">'
            + alert_toggles
            + '<div class="hub-field"><label for="pref-hour">Brief hour (0–23)</label>'
            + f'<input id="pref-hour" name="hour" type="number" min="0" max="23" value="{prefs["hour"]}" required></div>'
            + '<div class="hub-field"><label for="pref-tz">Timezone</label>'
            + _render_timezone_select(tz_name)
            + "</div>"
            + '<div class="hub-actions">'
            + button("Save preferences")
            + '<a class="button secondary" href="/connections/?view=preview">Preview next brief</a>'
            + "</div>"
            + "</form></div></article>"
        )

        deliveries_rows = accounts.recent_deliveries(settings.db, profile["person"], limit=20)
        kind_labels = {
            "results": "Results",
            "brief": "Daily brief",
            "weekly_brief": "Weekly brief",
            "urgent": "Urgent",
        }
        state_labels = {
            "sent": ("Sent", "hub-badge hub-badge-confirmed"),
            "pending": ("Pending", "hub-badge"),
            "unknown": ("Waiting review", "hub-badge hub-badge-paused"),
            "suppressed": ("Suppressed", "hub-badge hub-badge-off"),
            "superseded": ("Superseded", "hub-badge hub-badge-off"),
        }
        try:
            user_tz = ZoneInfo(tz_name)
        except (ValueError, ZoneInfoNotFoundError):
            user_tz = ZoneInfo("UTC")
        if deliveries_rows:
            d_trs = []
            for drow in deliveries_rows:
                k_label = kind_labels.get(drow["kind"], drow["kind"])
                s_label, s_cls = state_labels.get(drow["state"], (drow["state"], "hub-badge"))
                sent_str = "—"
                if drow.get("sent_at"):
                    try:
                        dt = datetime.fromisoformat(drow["sent_at"])
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        sent_str = dt.astimezone(user_tz).strftime("%Y-%m-%d %H:%M %Z")
                    except ValueError:
                        sent_str = str(drow["sent_at"])[:19]
                d_trs.append(
                    "<tr>"
                    f"<td>{html.escape(k_label)}</td>"
                    f"<td><code>{html.escape(drow['key'])}</code></td>"
                    f'<td><span class="{s_cls}">{html.escape(s_label)}</span></td>'
                    f'<td class="muted">{html.escape(sent_str)}</td>'
                    "</tr>"
                )
            history_html = (
                '<section class="hub-section"><h2>Recent Earnings Deliveries</h2>'
                '<div class="hub-table-wrap"><table class="hub-table">'
                "<thead><tr><th>Type</th><th>Delivery key</th><th>Status</th><th>Sent</th></tr></thead>"
                f"<tbody>{''.join(d_trs)}</tbody></table></div></section>"
            )
        else:
            history_html = (
                '<section class="hub-section"><h2>Recent Earnings Deliveries</h2>'
                '<p class="muted">No notifications recorded yet.</p></section>'
            )

        if not news_enabled:
            return f'<div class="hub-cards">{alerts_card}</div>{history_html}'

        pause_until = str(prefs.get("news_pause_until") or "")
        if not prefs.get("news_digest"):
            status_badge = '<span class="hub-badge hub-badge-off">Off</span>'
        elif pause_until and pause_until >= str(local_today):
            status_badge = f'<span class="hub-badge hub-badge-paused">Paused until {html.escape(pause_until)}</span>'
        else:
            status_badge = '<span class="hub-badge hub-badge-confirmed">Active</span>'

        slot_a_local = format_utc_slot(23, 30, tz_name, ref_date=local_today)
        slot_b_local = format_utc_slot(11, 30, tz_name, ref_date=local_today)
        cadence_options = [
            ("twice_daily", f"Twice daily ({slot_a_local} and {slot_b_local})"),
            ("slot_a", f"Once daily · {slot_a_local} (23:30 UTC)"),
            ("slot_b", f"Once daily · {slot_b_local} (11:30 UTC)"),
        ]
        radios_html = "".join(
            '<label class="hub-check"><input type="radio" name="news_cadence" value="'
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
                '<div class="hub-field"><label for="news-scope">Company scope</label>'
                f'<select id="news-scope" name="news_scope">{scope_opts}</select></div>'
            )

        news_card = (
            '<article class="hub-card">'
            f'<div class="hub-card-head"><h2>Company News Digest</h2>{status_badge}</div>'
            '<p class="muted">Delivered to '
            + html.escape(email_label)
            + " · times shown in "
            + html.escape(tz_name)
            + ".</p>"
            '<div class="hub-stack"><form method="post" action="/connections/preferences">'
            + form_fields(profile, view="digests")
            + '<input type="hidden" name="section" value="news">'
            + '<label class="hub-check"><input type="checkbox" name="news_digest"'
            + (" checked" if prefs.get("news_digest") else "")
            + "> Enable Company News Digest</label>"
            + '<div class="hub-field"><label for="news-pause">Pause until (optional)</label>'
            + f'<input id="news-pause" type="date" name="news_pause_until" value="{html.escape(pause_until, quote=True)}"></div>'
            + f'<fieldset class="hub-radios"><legend>Delivery cadence</legend>{radios_html}</fieldset>'
            + '<label class="hub-check"><input type="checkbox" name="news_weekdays_only"'
            + (" checked" if prefs.get("news_weekdays_only") else "")
            + "> Weekdays only</label>"
            + scope_html
            + button("Save news digest")
            + "</form></div></article>"
        )
        return f'<div class="hub-cards">{alerts_card}{news_card}</div>{history_html}'

    def _render_preview_tab(service, profile, *, show_holdings: bool) -> str:
        tz_name = profile["timezone"]
        pending = notify.notices(settings, profile["person"], include_holdings=show_holdings)
        cards: list[str] = []

        for _key, kind, payload in pending:
            rendered = render_email(kind, payload, timezone_name=tz_name)
            cards.append(
                '<article class="hub-card">'
                f'<div class="hub-card-head"><h2>{html.escape(rendered.subject)}</h2>'
                f'<span class="hub-badge hub-badge-confirmed">{html.escape(kind)}</span></div>'
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
                '<article class="hub-card">'
                f'<div class="hub-card-head"><h2>{html.escape(rendered.subject)}</h2>'
                '<span class="hub-badge">Sample (none due right now)</span></div>'
                '<p class="muted">No notification is due at this moment (quiet day or before your brief hour). '
                "Here is what a 7-day brief looks like from your current follows:</p>"
                f'<pre class="hub-preview-text">{html.escape(rendered.text.strip())}</pre>'
                "</article>"
            )

        return (
            '<div class="hub-actions" style="margin-bottom:1rem">'
            '<a class="button secondary" href="/connections/?view=digests">← Back to Digests</a>'
            "</div>" + "".join(cards)
        )

    def _render_calendar_tab(service, profile) -> str:
        token = accounts.feed_token(settings.db, profile["person"], service.signing_key)
        link = settings.public_url.rstrip("/") + "/calendar/" + token + ".ics"
        encoded_link = quote(link, safe="")
        outlook_live = (
            "https://outlook.live.com/owa/?path=/calendar/action/compose"
            f"&rru=addsubscription&url={encoded_link}&name=Earnings"
        )
        webcal_btn = ""
        if link.startswith("https://"):
            webcal_link = "webcal://" + link.removeprefix("https://")
            webcal_btn = (
                f'<a class="button secondary" href="{html.escape(webcal_link, quote=True)}">'
                "Subscribe in calendar app</a>"
            )
        return (
            '<article class="hub-card">'
            "<h2>Calendar subscription</h2>"
            "<p>Subscribe to a read-only calendar of your companies. Calendar apps control refresh timing, which can take hours. Your private link grants access to these events; keep it private.</p>"
            '<div class="hub-actions">'
            f'<a class="button" href="{html.escape(outlook_live, quote=True)}" target="_blank" rel="noopener noreferrer">Add to Outlook</a>'
            f"{webcal_btn}"
            "</div>"
            '<p class="muted" style="margin-top:1.25rem">Private calendar link</p>'
            '<div class="hub-code-box">'
            f'<code id="calendar-link">{html.escape(link, quote=True)}</code>'
            '<button type="button" class="secondary" data-copy-code="calendar-link">Copy link</button>'
            "</div>"
            '<div class="hub-actions">'
            '<button type="button" class="danger" popovertarget="reset-feed-popover">Reset private link</button>'
            "</div>"
            '<div id="reset-feed-popover" popover>'
            "<h2>Reset private calendar link?</h2>"
            "<p>Existing calendar subscriptions using the current link will stop updating. You will need to subscribe again with the new link.</p>"
            '<div class="hub-actions">'
            '<form method="post" action="/connections/reset-feed">'
            + form_fields(profile, view="calendar")
            + button("Reset link", cls="danger")
            + "</form>"
            '<button type="button" class="secondary" popovertarget="reset-feed-popover" popovertargetaction="hide">Cancel</button>'
            "</div></div></article>"
        )

    async def home(request):
        try:
            person, info = await identity(request)
            if not person:
                return (
                    RedirectResponse("/connections/login", 303)
                    if auth
                    else Response("Browser sign-in is not configured.", 503)
                )
            service = open_service(settings, person)
            profile = accounts.profile(settings.db, person)
            page_seq = Catalogue(settings.db).status()["sequence"]
            seen = profile.get("seen_seq") or 0
            changed = accounts.changed_ids(settings.db, person, seen, page_seq)
            follows = PersonalStore(settings.db, person).follows()
            show_holdings = _is_portfolio_admitted(info, follows)
            news_enabled = settings.integrations.news_digest.enabled

            view = request.query_params.get("view", "upcoming")
            if view not in _VIEWS:
                view = "upcoming"
            filter_by = request.query_params.get("filter", "all")
            if filter_by not in _FILTERS:
                filter_by = "all"

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
                tab_body = _render_calendar_tab(service, profile)

            nav_view = "digests" if view == "preview" else view
            body = _HUB_STYLE + _render_tabs(nav_view) + tab_body
            if request.query_params.get("keep_seen") != "1" and view == "upcoming":
                accounts.mark_seen(settings.db, person, page_seq)
            return HTMLResponse(
                page(
                    "Earnings", body, intro="The dates and documents for companies you care about."
                ),
                headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
            )
        except EarningsError as exc:
            return HTMLResponse(
                page("Earnings is unavailable", "<p>" + html.escape(str(exc)) + "</p>"), 403
            )
        except Exception:
            return HTMLResponse(
                page(
                    "Earnings is temporarily unavailable",
                    "<p>Your companies and preferences are safe. Try again shortly.</p>",
                ),
                503,
            )

    async def change(request):
        try:
            person, info = await identity(request)
            if not person or not auth.origin_ok(request):
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
            if action == "preferences":
                section = str(form.get("section", "")).strip()
                updated = dict(current_profile["settings"])
                tz = current_profile["timezone"]
                if section == "news":
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
                else:
                    for key in ("results", "brief", "weekly_brief", "urgent"):
                        updated[key] = form.get(key) == "on"
                    updated["hour"] = int(str(form.get("hour")))
                    tz = str(form.get("timezone"))
                accounts.preferences(settings.db, person, revision, updated, tz)
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
            elif action == "mark-seen":
                current_seq = Catalogue(settings.db).status()["sequence"]
                raw_seq = str(form.get("seen_seq", "")).strip()
                target_seq = min(int(raw_seq), current_seq) if raw_seq else current_seq
                accounts.mark_seen(settings.db, person, max(0, target_seq))
                return RedirectResponse("/connections/", 303)
            elif action == "unwatch":
                PersonalStore(settings.db, person).unwatch(ticker, exchange, revision=revision)
            elif action in ("mute", "unmute"):
                accounts.mute(
                    settings.db, person, ticker, exchange, action == "mute", revision=revision
                )
            elif action in ("news-mute", "news-unmute"):
                accounts.news_mute(
                    settings.db, person, ticker, exchange, action == "news-mute", revision=revision
                )
            elif action == "reset-feed":
                accounts.feed_token(
                    settings.db,
                    person,
                    (settings.data_dir / "calendar.key").read_bytes(),
                    rotate=True,
                    revision=revision,
                )
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
                    "<p>Adds "
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
                    + '<form method="post" action="/connections/import-apply">'
                    + form_fields(accounts.profile(settings.db, person), view="companies")
                    + '<input type="hidden" name="handle" value="'
                    + html.escape(result["handle"], quote=True)
                    + '">'
                    + button("Replace uploaded holdings")
                    + '</form><a href="/connections/?view=companies">Cancel</a>'
                )
                return HTMLResponse(
                    page("Review holdings import", body), headers={"Cache-Control": "no-store"}
                )
            elif action == "import-apply":
                interests.apply(settings, person, str(form.get("handle", "")))
            else:
                return Response("Unknown action.", 404)
            return RedirectResponse(redirect_url, 303)
        except (EarningsError, ValueError, KeyError) as exc:
            back_view = ""
            try:
                back_view = str(form.get("view", "")).strip()
            except Exception:
                back_view = ""
            back_href = (
                f"/connections/?view={back_view}"
                if back_view in _VIEWS and back_view != "upcoming"
                else "/connections/"
            )
            return HTMLResponse(
                page(
                    "Please review your change",
                    "<p>"
                    + html.escape(str(exc))
                    + f'</p><a href="{back_href}">Return to Earnings</a>',
                ),
                409,
            )
        except Exception:
            return HTMLResponse(
                page(
                    "Could not save right now",
                    "<p>Refresh the page and try again. Your existing preferences are retained.</p>",
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
        try:
            payload = await _library_targets_payload(settings, directory)
        except Exception:
            return JSONResponse(
                {"error": "identity directory unavailable"},
                status_code=503,
                headers={"Cache-Control": "no-store"},
            )
        return JSONResponse(payload, status_code=200, headers={"Cache-Control": "no-store"})

    async def calendar_feed(request):
        person = accounts.feed_person(settings.db, request.path_params["token"])
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
            content = open_service(settings, person).calendar_ics(include_holdings=include_holdings)
            return Response(
                content,
                media_type="text/calendar",
                headers={
                    "Cache-Control": "private, no-store",
                    "Referrer-Policy": "no-referrer",
                    "X-Content-Type-Options": "nosniff",
                },
            )
        except Exception:
            return Response(status_code=503)

    async def health(request):
        try:
            result = integrity(settings.db)
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
    # All surfaces, including the bearer calendar URL and internal targets API, share one Host boundary.
    from starlette.middleware.trustedhost import TrustedHostMiddleware

    outer = TrustedHostMiddleware(wrapped, allowed_hosts=settings.allowed_hosts)
    setattr(outer, "earnings_app", app)
    return outer
