"""Pure stdlib HTML and plain-text email renderer for standalone notifications."""

from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


@dataclass(frozen=True)
class RenderedEmail:
    subject: str
    text: str
    html: str


_EMAIL_STYLE = """
body{margin:0;padding:0;background:#f5f7fa;color:#142030;font:15px/1.55 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif}
.wrap{max-width:640px;margin:24px auto;padding:24px;background:#ffffff;border:1px solid #d9e1ec;border-radius:12px}
h1{font-size:20px;line-height:1.3;margin:0 0 8px;color:#0f172a}
h2{font-size:15px;line-height:1.4;margin:20px 0 8px;color:#334155;text-transform:uppercase;letter-spacing:.04em}
p{margin:8px 0}
.muted{color:#5b6b80;font-size:13px}
.card{padding:12px 14px;border:1px solid #e2e8f0;border-radius:8px;margin:10px 0;background:#f8fafc}
.badge{display:inline-block;padding:2px 8px;border-radius:999px;font-size:12px;font-weight:600;background:#e2e8f0;color:#1e293b}
a{color:#1d4ed8;text-decoration:underline}
ul{margin:8px 0;padding-left:20px}
li{margin:6px 0}
@media (prefers-color-scheme:dark){
  body{background:#0f172a;color:#e2e8f0}
  .wrap{background:#1e293b;border-color:#334155}
  h1{color:#f8fafc}
  h2{color:#cbd5e1}
  .muted{color:#94a3b8}
  .card{background:#0f172a;border-color:#334155}
  .badge{background:#334155;color:#e2e8f0}
  a{color:#93c5fd}
}
""".strip()


def _safe_url(value: Any) -> str:
    if not isinstance(value, str) or not value:
        return ""
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in value):
        return ""
    try:
        parsed = urlsplit(value)
    except ValueError:
        return ""
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return ""
    return value


def _clean_subject(value: str) -> str:
    return " ".join(value.replace("\r", " ").replace("\n", " ").split())


def _format_event_time(event: dict[str, Any], timezone_name: str) -> str:
    date_str = str(event.get("date") or "")
    if not date_str:
        return ""
    try:
        tz = ZoneInfo(timezone_name)
    except (ValueError, ZoneInfoNotFoundError):
        tz = ZoneInfo("UTC")
    if event.get("release_time") and event.get("release_tz") and not event.get("timing_issue"):
        try:
            dt = (
                datetime.fromisoformat(f"{date_str}T{event['release_time']}")
                .replace(tzinfo=ZoneInfo(str(event["release_tz"])))
                .astimezone(tz)
            )
            return dt.strftime("%a %d %b %Y, %H:%M %Z")
        except (ValueError, ZoneInfoNotFoundError):
            pass
    try:
        label = datetime.fromisoformat(date_str).strftime("%a %d %b %Y")
    except ValueError:
        label = date_str
    hint = {
        "BMO": "before market open",
        "AMC": "after market close",
        "DURING": "during market hours",
    }.get(str(event.get("time_hint") or ""))
    return f"{label}, {hint}" if hint else label


def _event_title(event: dict[str, Any]) -> str:
    ticker = str(event.get("ticker") or "")
    period = str(event.get("period") or "")
    ev_type = str(event.get("type") or "earnings")
    kind_label = "results" if ev_type == "earnings" else ev_type.replace("-", " ")
    return " ".join(part for part in (ticker, period, kind_label) if part)


def _html_link(url: str, label: str) -> str:
    safe = _safe_url(url)
    escaped_label = html.escape(label)
    if not safe:
        return escaped_label
    return f'<a href="{html.escape(safe, quote=True)}" rel="noreferrer">{escaped_label}</a>'


def _wrap_html(title: str, subtitle: str, body_html: str) -> str:
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{html.escape(title)}</title><style>{_EMAIL_STYLE}</style></head>"
        '<body><div class="wrap">'
        f"<h1>{html.escape(title)}</h1>"
        + (f'<p class="muted">{html.escape(subtitle)}</p>' if subtitle else "")
        + body_html
        + "</div></body></html>"
    )


def _render_documents_html(documents: list[dict[str, Any]]) -> str:
    if not documents:
        return ""
    items = []
    for doc in documents:
        label = str(doc.get("title") or doc.get("role") or "Document")
        role = str(doc.get("role") or "")
        link = _html_link(str(doc.get("url") or ""), label)
        suffix = (
            f' <span class="muted">({html.escape(role)})</span>' if role and role != label else ""
        )
        items.append(f"<li>{link}{suffix}</li>")
    return "<ul>" + "".join(items) + "</ul>"


def _render_documents_text(documents: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for doc in documents:
        label = str(doc.get("title") or doc.get("role") or "Document")
        url = _safe_url(doc.get("url"))
        lines.append(f"  - {label}: {url}" if url else f"  - {label}")
    return lines


def _render_results(payload: dict[str, Any], timezone_name: str) -> RenderedEmail:
    event = payload.get("event") or {}
    documents = payload.get("documents") or []
    title = _event_title(event)
    subject = _clean_subject(f"Results out: {title}")
    when = _format_event_time(event, timezone_name)
    source_url = _safe_url(event.get("source_url"))

    text_lines = [subject, ""]
    if when:
        text_lines.append(f"Scheduled: {when}")
    if source_url:
        text_lines.append(f"Source: {source_url}")
    if documents:
        text_lines.append("")
        text_lines.append("Verified documents:")
        text_lines.extend(_render_documents_text(documents))

    body_parts = ['<div class="card">']
    body_parts.append(f"<p><strong>{html.escape(title)}</strong></p>")
    if when:
        body_parts.append(f'<p class="muted">{html.escape(when)}</p>')
    if source_url:
        body_parts.append(f"<p>{_html_link(source_url, 'Official source')}</p>")
    body_parts.append("</div>")
    if documents:
        body_parts.append("<h2>Verified documents</h2>")
        body_parts.append(_render_documents_html(documents))

    return RenderedEmail(
        subject=subject,
        text="\n".join(text_lines).strip() + "\n",
        html=_wrap_html(title, f"Results out · {timezone_name}", "".join(body_parts)),
    )


def _render_urgent(payload: dict[str, Any], timezone_name: str) -> RenderedEmail:
    event = payload.get("event") or {}
    previous = payload.get("previous") or {}
    change = str(payload.get("change") or "updated")
    title = _event_title(event or previous)
    subject = _clean_subject(f"Urgent timing change: {title}")
    prev_when = _format_event_time(previous, timezone_name)
    curr_when = (
        "Removed from issuer calendar"
        if change == "removed"
        else _format_event_time(event, timezone_name)
    )
    source_url = _safe_url(event.get("source_url") or previous.get("source_url"))

    text_lines = [
        subject,
        "",
        f"Change: {change}",
        f"Previous: {prev_when or 'Unknown'}",
        f"Current: {curr_when or 'Unknown'}",
    ]
    if source_url:
        text_lines.append(f"Source: {source_url}")

    body_parts = [
        '<div class="card">',
        f'<p><span class="badge">{html.escape(change.upper())}</span> <strong>{html.escape(title)}</strong></p>',
        f"<p><strong>Previous:</strong> {html.escape(prev_when or 'Unknown')}</p>",
        f"<p><strong>Current:</strong> {html.escape(curr_when or 'Unknown')}</p>",
    ]
    if source_url:
        body_parts.append(f"<p>{_html_link(source_url, 'Official source')}</p>")
    body_parts.append("</div>")

    return RenderedEmail(
        subject=subject,
        text="\n".join(text_lines).strip() + "\n",
        html=_wrap_html(subject, timezone_name, "".join(body_parts)),
    )


def _render_brief(kind: str, payload: dict[str, Any], timezone_name: str) -> RenderedEmail:
    is_weekly = kind == "weekly_brief"
    day = str(payload.get("week") or payload.get("day") or "")
    heading = "Weekly earnings brief" if is_weekly else "Daily earnings brief"
    subject = _clean_subject(f"{heading} · {day}" if day else heading)
    events = payload.get("events") or []
    changes = payload.get("changes") or []
    documents = payload.get("documents") or []

    text_lines = [subject, ""]
    body_parts: list[str] = []

    if events:
        text_lines.append("Upcoming events:")
        body_parts.append("<h2>Upcoming events</h2>")
        for ev in events:
            ev_title = _event_title(ev)
            when = _format_event_time(ev, timezone_name)
            source_url = _safe_url(ev.get("source_url"))
            status = str(ev.get("status") or "")
            text_lines.append(
                f"  - {ev_title} ({when})" + (f" [{source_url}]" if source_url else "")
            )
            link_html = f" · {_html_link(source_url, 'Source')}" if source_url else ""
            badge = f' <span class="badge">{html.escape(status)}</span>' if status else ""
            body_parts.append(
                '<div class="card">'
                f"<strong>{html.escape(ev_title)}</strong>{badge}"
                f'<p class="muted">{html.escape(when)}{link_html}</p>'
                "</div>"
            )
        text_lines.append("")

    if documents:
        text_lines.append("New verified documents:")
        text_lines.extend(_render_documents_text(documents))
        text_lines.append("")
        body_parts.append("<h2>New verified documents</h2>")
        body_parts.append(_render_documents_html(documents))

    if changes:
        text_lines.append("Recent calendar changes:")
        body_parts.append("<h2>Recent calendar changes</h2><ul>")
        for ch in changes:
            doc = ch.get("doc") or {}
            ch_title = _event_title(doc)
            ch_kind = str(ch.get("change") or "updated")
            when = _format_event_time(doc, timezone_name)
            source_url = _safe_url(doc.get("source_url"))
            text_lines.append(f"  - [{ch_kind}] {ch_title} ({when})")
            link_html = f" · {_html_link(source_url, 'Source')}" if source_url else ""
            body_parts.append(
                f'<li><span class="badge">{html.escape(ch_kind)}</span> '
                f"<strong>{html.escape(ch_title)}</strong> — {html.escape(when)}{link_html}</li>"
            )
        body_parts.append("</ul>")

    if not body_parts:
        text_lines.append("No new items.")
        body_parts.append("<p>No new items.</p>")

    return RenderedEmail(
        subject=subject,
        text="\n".join(text_lines).strip() + "\n",
        html=_wrap_html(
            heading, f"{day} · {timezone_name}" if day else timezone_name, "".join(body_parts)
        ),
    )


def render_email(
    kind: str, payload: dict[str, Any], *, timezone_name: str = "UTC"
) -> RenderedEmail:
    """Render a notification payload into subject, plain text, and dark/light HTML."""
    if kind == "results":
        return _render_results(payload, timezone_name)
    if kind == "urgent":
        return _render_urgent(payload, timezone_name)
    if kind in {"brief", "weekly_brief"}:
        return _render_brief(kind, payload, timezone_name)
    raise ValueError(f"unsupported email notification kind: {kind}")
