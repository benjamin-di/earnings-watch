"""Connections UI and private calendar subscriptions over the native application."""

from __future__ import annotations

import html
import os
from contextlib import asynccontextmanager
from urllib.parse import quote

from signin_window.connections import page
from signin_window.oidc import BrowserAuth, OIDCConfig
from starlette.applications import Starlette
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.datastructures import UploadFile
from starlette.routing import Mount, Route, BaseRoute

from .composition import open_service
from .errors import EarningsError
from . import interests
from .http_server import AuthenticatedApp
from .store import accounts
from .store.database import Catalogue, LibraryReader, PersonalStore, integrity
from .transport import IdentityDirectory

MAX_BODY = 65536


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
            )
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

    def form_fields(profile):
        return '<input type="hidden" name="revision" value="' + str(profile["revision"]) + '">'

    def button(label):
        return '<button type="submit">' + html.escape(label) + "</button>"

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
            current = service.upcoming(days=30)
            seen = profile.get("seen_seq") or 0
            changed = accounts.changed_ids(settings.db, person, seen, page_seq)
            cards = []
            for e in current["events"]:
                links = " ".join(
                    '<a href="'
                    + html.escape(d["url"], quote=True)
                    + '" rel="noreferrer">'
                    + html.escape(d.get("title") or d["role"])
                    + "</a>"
                    for d in e["documents"]
                )
                cards.append(
                    "<li><strong>"
                    + html.escape(e["title"])
                    + "</strong> "
                    + ('<span class="status">Changed</span>' if e["id"] in changed else "")
                    + "<p>"
                    + html.escape(e["when"])
                    + " · "
                    + html.escape(e["certainty"])
                    + '</p><a href="'
                    + html.escape(e["source_url"], quote=True)
                    + '" rel="noreferrer">Source</a> '
                    + links
                    + "</li>"
                )
            upcoming = (
                '<ul class="events">' + "".join(cards) + "</ul>"
                if cards
                else "<p>No upcoming events yet. Add a company below to get started.</p>"
            )
            follows = PersonalStore(settings.db, person).follows()
            muted = accounts.mutes(settings.db, person)
            companies = []
            for row in follows:
                state = "Holding" if row["origin"].startswith("holdings:") else "Watchlist"
                feed = row["origin"].removeprefix("holdings:")
                synced = profile["holdings_sync"].get(feed)
                if row["origin"].startswith("holdings:") and feed != "upload" and synced:
                    state = "Holding · Synced from your portfolio, " + synced[:10]
                if row["state"] == "queued":
                    state = "Adding · usually within a day"
                is_muted = (row["ticker"], row["exchange"]) in muted or (row["ticker"], "") in muted
                fields = (
                    form_fields(profile)
                    + '<input type="hidden" name="ticker" value="'
                    + html.escape(row["ticker"], quote=True)
                    + '"><input type="hidden" name="exchange" value="'
                    + html.escape(row["exchange"], quote=True)
                    + '">'
                )
                action = (
                    "unmute"
                    if is_muted
                    else ("mute" if row["origin"].startswith("holdings:") else "unwatch")
                )
                companies.append(
                    "<li><strong>"
                    + html.escape(row["ticker"])
                    + "</strong> "
                    + html.escape(row["exchange"])
                    + " · "
                    + state
                    + (" · Muted" if is_muted else "")
                    + '<form method="post" action="/connections/'
                    + action
                    + '">'
                    + fields
                    + button("Unmute" if is_muted else ("Mute" if action == "mute" else "Remove"))
                    + "</form></li>"
                )
            options = "".join(
                '<option value="'
                + html.escape(s.ticker, quote=True)
                + '">'
                + html.escape(s.name + " · " + s.ticker + " · " + (s.exchange or ""))
                + "</option>"
                for s in LibraryReader(settings.db).sources()
            )
            add = (
                '<form method="post" action="/connections/watch">'
                + form_fields(profile)
                + '<label>Company ticker or ISIN <input name="ticker" list="companies" required maxlength="20" autocomplete="off"></label><datalist id="companies">'
                + options
                + '</datalist><label>Exchange (optional MIC)<input name="exchange" maxlength="4"></label>'
                + button("Add to watchlist")
                + "<p>Not listed? Add its ticker to request it. Up to five requests can be pending.</p></form>"
            )
            upload = (
                '<details><summary>Import holdings</summary><p>Paste tickers or MIC:TICKER, or upload a CSV with ticker and exchange columns. Quantities and values are discarded. Review changes before replacing uploaded holdings; your synced portfolio and watchlist are kept.</p><form method="post" action="/connections/import-preview" enctype="multipart/form-data">'
                + form_fields(profile)
                + '<label>Companies<textarea name="companies" rows="3" maxlength="32768"></textarea></label><label>CSV file<input name="file" type="file" accept=".csv,text/csv"></label>'
                + button("Preview import")
                + "</form></details>"
            )
            preferences = profile["settings"]
            toggles = "".join(
                '<label><input type="checkbox" name="'
                + k
                + '"'
                + (" checked" if preferences[k] else "")
                + "> "
                + v
                + "</label>"
                for k, v in [
                    ("results", "Results out"),
                    ("brief", "Daily brief, only when useful"),
                    ("urgent", "Urgent date changes"),
                ]
            )
            settings_form = (
                "<p>Sent to "
                + html.escape(
                    info.get("email") or "No email is available from your identity provider"
                )
                + ' (your sign-in email). Change this through your identity provider.</p><form method="post" action="/connections/preferences">'
                + form_fields(profile)
                + toggles
                + '<label>Brief hour (0–23)<input name="hour" type="number" min="0" max="23" value="'
                + str(preferences["hour"])
                + '" required></label><label>Timezone<input name="timezone" value="'
                + html.escape(profile["timezone"], quote=True)
                + '" required list="timezones"></label><datalist id="timezones"><option>UTC</option><option>Europe/Berlin</option><option>Asia/Hong_Kong</option><option>America/New_York</option></datalist>'
                + button("Save preferences")
                + "</form>"
            )
            token = accounts.feed_token(settings.db, person, service.signing_key)
            link = settings.public_url.rstrip("/") + "/calendar/" + token + ".ics"
            outlook = "https://outlook.live.com/calendar/0/addcalendar?url=" + quote(link, safe="")
            calendar = (
                '<p>Subscribe to a read-only calendar of your companies. Calendar apps control refresh timing, which can take hours. Your private link grants access to these events; keep it private.</p><a class="button" href="'
                + html.escape(outlook, quote=True)
                + '" rel="noreferrer">Add to Outlook</a><p>Private calendar link</p><code id="calendar-link">'
                + html.escape(link, quote=True)
                + '</code><button type="button" data-copy-code="calendar-link">Copy link</button><details><summary>Reset private link</summary><p>Existing subscriptions will stop working. Subscribe again with the new link.</p><form method="post" action="/connections/reset-feed">'
                + form_fields(profile)
                + button("Reset link")
                + "</form></details>"
            )
            body = '<nav aria-label="Page sections"><a href="#upcoming">Upcoming</a> · <a href="#companies">Your companies</a> · <a href="#notifications">Notifications</a> · <a href="#calendar">Calendar</a></nav>'
            note = current.get("coverage_note", "")
            if note:
                body += '<p role="status">' + html.escape(note) + "</p>"
            body += (
                '<section id="upcoming"><h2>Upcoming</h2><p>Next 30 days · '
                + html.escape(profile["timezone"])
                + "</p>"
                + upcoming
                + '</section><section id="companies"><h2>Your companies</h2><ul>'
                + "".join(companies)
                + "</ul>"
                + add
                + upload
                + '</section><section id="notifications"><h2>Notifications</h2>'
                + settings_form
                + '</section><section id="calendar"><h2>Calendar</h2>'
                + calendar
                + '</section><form method="post" action="/connections/logout">'
                + button("Sign out")
                + "</form>"
            )
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
            person, _ = await identity(request)
            if not person or not auth.origin_ok(request):
                return Response("Sign in and refresh this page before saving.", 403)
            # Stream first, so chunked bodies cannot bypass the bound.
            data = bytearray()
            async for chunk in request.stream():
                data.extend(chunk)
                if len(data) > MAX_BODY:
                    return Response("Import is too large.", 413)
            request._body = bytes(data)
            form = await request.form(max_files=1, max_fields=10, max_part_size=MAX_BODY)
            revision = int(str(form.get("revision", "-1")))
            if revision != accounts.profile(settings.db, person)["revision"]:
                raise EarningsError("conflict", "This page changed. Refresh before saving.")
            action = request.path_params["action"]
            ticker = str(form.get("ticker", "")).upper().strip()
            exchange = str(form.get("exchange", "")).upper().strip()
            if action == "preferences":
                accounts.preferences(
                    settings.db,
                    person,
                    revision,
                    {k: form.get(k) == "on" for k in ("results", "brief", "urgent")}
                    | {"hour": int(str(form.get("hour")))},
                    str(form.get("timezone")),
                )
            elif action == "watch":
                PersonalStore(settings.db, person).watch(
                    ticker, exchange, isin=ticker if len(ticker) == 12 else None, revision=revision
                )
            elif action == "unwatch":
                PersonalStore(settings.db, person).unwatch(ticker, exchange, revision=revision)
            elif action in ("mute", "unmute"):
                accounts.mute(
                    settings.db, person, ticker, exchange, action == "mute", revision=revision
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
                    text = (await upload.read(MAX_BODY)).decode("utf-8-sig")
                result = interests.preview(settings, person, text)
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
                    + '</ul><form method="post" action="/connections/import-apply">'
                    + form_fields(accounts.profile(settings.db, person))
                    + '<input type="hidden" name="handle" value="'
                    + html.escape(result["handle"], quote=True)
                    + '">'
                    + button("Replace uploaded holdings")
                    + '</form><a href="/connections/">Cancel</a>'
                )
                return HTMLResponse(
                    page("Review holdings import", body), headers={"Cache-Control": "no-store"}
                )
            elif action == "import-apply":
                interests.apply(settings, person, str(form.get("handle", "")))
            else:
                return Response("Unknown action.", 404)
            return RedirectResponse("/connections/", 303)
        except (EarningsError, ValueError, KeyError) as exc:
            return HTMLResponse(
                page(
                    "Please review your change",
                    "<p>"
                    + html.escape(str(exc))
                    + '</p><a href="/connections/">Return to Earnings</a>',
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

    async def calendar_feed(request):
        person = accounts.feed_person(settings.db, request.path_params["token"])
        if not person:
            return Response(status_code=404)
        try:
            if directory:
                who = await directory.resolve(person)
                if not who["admitted"] or who["disabled"]:
                    return Response(status_code=404)
            elif settings.directory_required:
                return Response(status_code=503)
            content = open_service(settings, person).calendar_ics()
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
            return JSONResponse({"integrity": integrity(settings.db)})
        except Exception:
            return JSONResponse({"error": "unavailable"}, 503)

    @asynccontextmanager
    async def lifespan(app):
        async with native.app.router.lifespan_context(native.app):
            yield

    routes: list[BaseRoute] = [
        Route("/", lambda request: RedirectResponse("/connections/", 303)),
        Route("/health", health),
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
    # All surfaces, including the bearer calendar URL, share one Host boundary.
    from starlette.middleware.trustedhost import TrustedHostMiddleware

    outer = TrustedHostMiddleware(wrapped, allowed_hosts=settings.allowed_hosts)
    setattr(outer, "earnings_app", app)
    return outer
