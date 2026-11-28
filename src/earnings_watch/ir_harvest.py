"""2-phase IR asset harvester: Map Once, Classify Link Deltas in 1 Shot."""

from __future__ import annotations

import html
import json
import re
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlencode, urljoin, urlsplit

from pydantic import BaseModel, Field

from earnings_watch.llm.runner import AgentRunner, AgentTask
from earnings_watch.model.sources import Source
from earnings_watch.model.urls import apex_domain, public_url, same_apex
from earnings_watch.providers.q4_ir import _parse_q4_datetime, is_q4_source
from earnings_watch.store.database import Catalogue, digest
from earnings_watch.transport import Transport

_CHROME_TAGS = frozenset(
    {"header", "nav", "footer", "script", "style", "noscript", "svg", "template"}
)
_CONTAINER_TAGS = frozenset({"tr", "li", "article", "section", "div"})
_DIRECT_MEDIA_EXT_RE = re.compile(r"\.(?:mp3|m4a|wav|ogg|opus|mp4|m3u8)$", re.I)
_DIRECT_MEDIA_HOSTS = frozenset(
    {"youtube.com", "www.youtube.com", "youtu.be", "vimeo.com", "player.vimeo.com"}
)
_ALLOWED_CATEGORIES = frozenset(
    {
        "presentation",
        "transcript",
        "results_release",
        "databook",
        "audio_replay",
        "webcast_page",
        "annual_report",
        "interim_report",
        "quarterly_report",
    }
)
_MAX_SEEN_URLS = 1000


def is_direct_media_url(url: str) -> bool:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if host in _DIRECT_MEDIA_HOSTS:
        return True
    return bool(_DIRECT_MEDIA_EXT_RE.search(parsed.path))


@dataclass(frozen=True)
class LinkManifestEntry:
    url: str
    text: str
    context: str


class _ManifestParser(HTMLParser):
    def __init__(self, base_url: str, *, max_context_chars: int = 180) -> None:
        super().__init__(convert_charrefs=True)
        self._base_url = base_url
        self._max_context_chars = max_context_chars
        self._skip_depth = 0
        self._containers: list[tuple[str, list[str], list[tuple[str, str]]]] = []
        self._in_anchor_href: str | None = None
        self._anchor_text: list[str] = []
        self._anchor_fallback: str = ""
        self.entries: list[LinkManifestEntry] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lower = tag.lower()
        if lower in _CHROME_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth > 0:
            return
        if lower in _CONTAINER_TAGS:
            self._containers.append((lower, [], []))
        elif lower == "a":
            attr_map = dict(attrs)
            href = (attr_map.get("href") or "").strip()
            if href and not href.startswith("#"):
                self._in_anchor_href = href
                self._anchor_text = []
                self._anchor_fallback = " ".join(
                    (attr_map.get("aria-label") or attr_map.get("title") or "").split()
                )[:120]

    def handle_data(self, data: str) -> None:
        if self._skip_depth > 0:
            return
        if self._in_anchor_href is not None:
            self._anchor_text.append(data)
        for _, buf, _ in self._containers:
            buf.append(data)

    def handle_endtag(self, tag: str) -> None:
        lower = tag.lower()
        if lower in _CHROME_TAGS:
            if self._skip_depth > 0:
                self._skip_depth -= 1
            return
        if self._skip_depth > 0:
            return
        if lower == "a" and self._in_anchor_href is not None:
            raw_href = self._in_anchor_href
            text = " ".join("".join(self._anchor_text).split())[:120] or self._anchor_fallback
            self._in_anchor_href = None
            self._anchor_text = []
            self._anchor_fallback = ""
            try:
                resolved = public_url(urljoin(self._base_url, raw_href))
            except ValueError:
                return
            if self._containers:
                self._containers[-1][2].append((resolved, text))
            else:
                self.entries.append(LinkManifestEntry(url=resolved, text=text, context=text))
        elif lower in _CONTAINER_TAGS and self._containers:
            # Pop matching container (tolerant of unclosed tags)
            idx = len(self._containers) - 1
            while idx >= 0 and self._containers[idx][0] != lower:
                idx -= 1
            if idx < 0:
                return
            _, text_buf, pending_links = self._containers.pop(idx)
            ctx = " ".join("".join(text_buf).split())[: self._max_context_chars]
            for resolved, link_text in pending_links:
                self.entries.append(
                    LinkManifestEntry(url=resolved, text=link_text, context=ctx or link_text)
                )

    def finalize(self) -> None:
        while self._containers:
            _, text_buf, pending_links = self._containers.pop()
            ctx = " ".join("".join(text_buf).split())[: self._max_context_chars]
            for resolved, link_text in pending_links:
                self.entries.append(
                    LinkManifestEntry(url=resolved, text=link_text, context=ctx or link_text)
                )


def extract_link_manifest(
    url: str,
    html_body: str,
    *,
    seen_urls: Collection[str] | None = None,
    max_links: int = 200,
    max_context_chars: int = 180,
) -> tuple[LinkManifestEntry, ...]:
    """Strip nav/header/footer chrome and return deduplicated new links with container context."""
    parser = _ManifestParser(url, max_context_chars=max_context_chars)
    parser.feed(html_body)
    parser.finalize()
    already_seen = set(seen_urls) if seen_urls else set()
    emitted: set[str] = set()
    result: list[LinkManifestEntry] = []
    for entry in parser.entries:
        if entry.url in emitted or entry.url in already_seen:
            continue
        emitted.add(entry.url)
        result.append(entry)
        if len(result) >= max_links:
            break
    return tuple(result)


def _source_hosts(source: Source) -> set[str]:
    return {
        h.lower()
        for u in (
            source.calendar_url,
            source.results_url,
            source.announcements_url,
            source.presentations_url,
            source.transcripts_url,
            source.archive_url,
        )
        if u and (h := urlsplit(u).hostname)
    }


def _is_official_page(source: Source, page_url: str) -> bool:
    try:
        norm = public_url(page_url)
    except ValueError:
        return False
    host = (urlsplit(norm).hostname or "").lower()
    if not host:
        return False
    registered = _source_hosts(source)
    return host in registered or any(same_apex(host, reg) for reg in registered)


class _DeltaItem(BaseModel):
    url: str
    category: str
    title: str
    published_date: str | None = None
    period: str | None = None
    evidence: str


class _DeltaBatch(BaseModel):
    items: list[_DeltaItem] = Field(default_factory=list, max_length=100)


async def classify_ir_delta(
    source: Source,
    page_url: str,
    manifest: Sequence[LinkManifestEntry],
    runner: AgentRunner,
    *,
    event_id: str | None = None,
    model: str | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Classify newly discovered IR links in 1 non-agentic JSON call (0 calls if empty)."""
    if not manifest or not _is_official_page(source, page_url):
        return [], 0

    by_url = {entry.url: entry for entry in manifest}
    compact_links = [
        {"url": entry.url, "text": entry.text, "context": entry.context} for entry in manifest
    ]
    result = await runner.run(
        AgentTask(
            kind="json",
            max_turns=1,
            system=(
                "Classify newly discovered investor-relations links for this issuer. "
                "Include only official presentation, transcript, results_release, databook, "
                "audio_replay (direct mp3/m4a/mp4/m3u8/YouTube/Vimeo), or webcast_page links. "
                "Ignore navigation, legal, social, and unrelated links. "
                "Quote evidence verbatim from text or context. Never invent URLs."
            ),
            prompt=json.dumps(
                {
                    "issuer": {"key": source.key, "ticker": source.ticker, "name": source.name},
                    "page_url": page_url,
                    "links": compact_links,
                }
            ),
            schema=_DeltaBatch,
            model=model,
        )
    )
    calls = 2 if result.repaired else 1
    if result.parsed is None:
        return [], calls

    parsed = _DeltaBatch.model_validate(result.parsed)
    now_iso = datetime.now(timezone.utc).isoformat()
    verified: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in parsed.items:
        try:
            norm_url = public_url(item.url)
        except ValueError:
            continue
        entry = by_url.get(norm_url)
        if entry is None or norm_url in seen:
            continue
        category = item.category.strip().lower().replace("-", "_").replace(" ", "_")
        if category not in _ALLOWED_CATEGORIES:
            continue
        haystack = " ".join(f"{entry.text} {entry.context}".split()).casefold()
        needle = " ".join(item.evidence.split()).casefold()
        if not needle or needle not in haystack:
            continue
        seen.add(norm_url)
        verified.append(
            {
                "id": digest([source.key, norm_url])[:24],
                "source_key": source.key,
                "event_id": event_id,
                "url": norm_url,
                "category": category,
                "role": category,
                "title": (item.title.strip() or entry.text)[:120],
                "published_date": item.published_date,
                "period": item.period,
                "evidence": item.evidence[:500],
                "evidence_url": page_url,
                "first_seen": now_iso,
            }
        )
    return verified, calls


async def discover_q4_ir_assets(
    transport: Transport,
    source: Source,
    *,
    today: date | None = None,
    lookback_days: int = 365,
) -> list[dict[str, Any]]:
    """Deterministically harvest presentations, transcripts, and webcasts from Q4 JSON feeds."""
    seed = source.presentations_url or source.results_url or source.calendar_url
    host = (urlsplit(seed).hostname or "").lower()
    if not host:
        return []
    base = f"https://{host}"
    ref_date = today or datetime.now(timezone.utc).date()
    cutoff = ref_date - timedelta(days=lookback_days)
    years = sorted(range(cutoff.year, ref_date.year + 1), reverse=True)
    now_iso = datetime.now(timezone.utc).isoformat()
    assets: list[dict[str, Any]] = []
    seen_urls: set[str] = set()

    async def _get_json(url: str) -> dict[str, Any] | None:
        try:
            resp = await transport.get(url)
        except Exception:
            return None
        if resp.status == 304 or not resp.body:
            return None
        try:
            data = json.loads(resp.body)
        except (ValueError, UnicodeDecodeError):
            return None
        return data if isinstance(data, dict) else None

    for year in years:
        pres_url = f"{base}/feed/Presentation.svc/GetPresentationList?" + urlencode(
            {
                "LanguageId": 1,
                "bodyType": 0,
                "presentationDateFilter": 3,
                "pageSize": 50,
                "pageNumber": 0,
                "year": year,
            }
        )
        payload = await _get_json(pres_url)
        for item in (payload or {}).get("GetPresentationListResult") or []:
            if not isinstance(item, dict):
                continue
            title = " ".join(html.unescape(str(item.get("Title") or "")).split())
            dt_info = _parse_q4_datetime(item.get("PresentationDate"))
            raw_doc = str(item.get("DocumentPath") or "").strip()
            if not title or dt_info is None or dt_info[0] < cutoff or not raw_doc:
                continue
            try:
                doc_url = public_url(urljoin(base, raw_doc))
            except ValueError:
                continue
            if doc_url in seen_urls:
                continue
            seen_urls.add(doc_url)
            assets.append(
                {
                    "id": digest([source.key, doc_url])[:24],
                    "source_key": source.key,
                    "event_id": None,
                    "url": doc_url,
                    "category": "presentation",
                    "role": "presentation",
                    "title": title[:120],
                    "published_date": dt_info[0].isoformat(),
                    "period": None,
                    "evidence": title[:500],
                    "evidence_url": pres_url,
                    "first_seen": now_iso,
                }
            )

        ev_url = f"{base}/feed/Event.svc/GetEventList?" + urlencode(
            {
                "LanguageId": 1,
                "eventDateFilter": 3,
                "pageSize": 50,
                "pageNumber": 0,
                "year": year,
            }
        )
        ev_payload = await _get_json(ev_url)
        for item in (ev_payload or {}).get("GetEventListResult") or []:
            if not isinstance(item, dict):
                continue
            ev_title = " ".join(html.unescape(str(item.get("Title") or "")).split())
            dt_info = _parse_q4_datetime(item.get("StartDate"))
            if not ev_title or dt_info is None or dt_info[0] < cutoff:
                continue
            pub_date = dt_info[0].isoformat()
            for att in item.get("Attachments") or []:
                if not isinstance(att, dict):
                    continue
                att_title = " ".join(html.unescape(str(att.get("Title") or ev_title)).split())
                raw_att = str(att.get("Url") or "").strip()
                if not raw_att:
                    continue
                try:
                    att_url = public_url(urljoin(base, raw_att))
                except ValueError:
                    continue
                if att_url in seen_urls:
                    continue
                lower_meta = f"{att_title} {att.get('Type') or ''}".lower()
                if "transcript" in lower_meta or "prepared remarks" in lower_meta:
                    cat = "transcript"
                elif any(k in lower_meta for k in ("presentation", "slide", "deck")):
                    cat = "presentation"
                else:
                    continue
                seen_urls.add(att_url)
                assets.append(
                    {
                        "id": digest([source.key, att_url])[:24],
                        "source_key": source.key,
                        "event_id": None,
                        "url": att_url,
                        "category": cat,
                        "role": cat,
                        "title": att_title[:120],
                        "published_date": pub_date,
                        "period": None,
                        "evidence": att_title[:500],
                        "evidence_url": ev_url,
                        "first_seen": now_iso,
                    }
                )
            raw_wc = str(item.get("WebCastLink") or "").strip()
            if raw_wc:
                try:
                    wc_url = public_url(urljoin(base, raw_wc))
                except ValueError:
                    wc_url = ""
                if wc_url and wc_url not in seen_urls:
                    seen_urls.add(wc_url)
                    cat = "audio_replay" if is_direct_media_url(wc_url) else "webcast_page"
                    assets.append(
                        {
                            "id": digest([source.key, wc_url])[:24],
                            "source_key": source.key,
                            "event_id": None,
                            "url": wc_url,
                            "category": cat,
                            "role": cat,
                            "title": ev_title[:120],
                            "published_date": pub_date,
                            "period": None,
                            "evidence": ev_title[:500],
                            "evidence_url": ev_url,
                            "first_seen": now_iso,
                        }
                    )
    return assets


async def harvest_ir_assets(
    catalogue: Catalogue,
    source: Source,
    transport: Transport,
    *,
    runner: AgentRunner | None = None,
    browser: Any | None = None,
    event_id: str | None = None,
    dry_run: bool = False,
    model: str | None = None,
    today: date | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Run deterministic Q4 + delta-manifest 1-shot LLM IR asset harvest for one issuer."""
    found: list[dict[str, Any]] = []
    calls_used = 0

    if source.ir_platform == "q4" or is_q4_source(source):
        q4_assets = await discover_q4_ir_assets(transport, source, today=today)
        if event_id:
            for a in q4_assets:
                a["event_id"] = a.get("event_id") or event_id
        found.extend(q4_assets)

    prev_state = catalogue.state("fetch_state", source.key)
    seen_list: list[str] = list(prev_state.get("ir_seen_urls") or [])
    seen_set = set(seen_list)

    page_urls: list[str] = []
    for raw in (
        source.presentations_url,
        source.transcripts_url,
        source.results_url,
        source.archive_url,
    ):
        if raw and raw not in page_urls:
            page_urls.append(raw)
    if not page_urls and source.calendar_url:
        page_urls.append(source.calendar_url)

    new_seen: list[str] = []
    hosts = _source_hosts(source)
    apex_domains = {apex for h in hosts if (apex := apex_domain(h))}
    for page_url in page_urls[:4]:
        html_body = ""
        final_url = page_url
        if source.render_mode == "browser" and browser is not None:
            try:
                cap = await browser.read(page_url, allow_apex=apex_domains, allow_hosts=hosts)
                final_url = cap.url
                manifest = tuple(
                    LinkManifestEntry(
                        url=str(item["url"]),
                        text=str(item.get("title") or "")[:120],
                        context=str(item.get("title") or "")[:180],
                    )
                    for item in cap.links
                    if isinstance(item, dict)
                    and item.get("url")
                    and str(item["url"]) not in seen_set
                )
            except Exception:
                continue
        else:
            try:
                resp = await transport.get(page_url)
            except Exception:
                if browser is not None:
                    try:
                        cap = await browser.read(
                            page_url, allow_apex=apex_domains, allow_hosts=hosts
                        )
                        final_url = cap.url
                        manifest = tuple(
                            LinkManifestEntry(
                                url=str(item["url"]),
                                text=str(item.get("title") or "")[:120],
                                context=str(item.get("title") or "")[:180],
                            )
                            for item in cap.links
                            if isinstance(item, dict)
                            and item.get("url")
                            and str(item["url"]) not in seen_set
                        )
                    except Exception:
                        continue
                else:
                    continue
            else:
                if resp.status == 304 or not resp.body:
                    continue
                if "html" not in resp.content_type.lower():
                    continue
                final_url = resp.url
                html_body = resp.body.decode("utf-8", errors="replace")
                manifest = extract_link_manifest(final_url, html_body, seen_urls=seen_set)

        if not manifest:
            continue
        for entry in manifest:
            if entry.url not in seen_set:
                seen_set.add(entry.url)
                new_seen.append(entry.url)

        if runner is not None:
            classified, used = await classify_ir_delta(
                source,
                final_url,
                manifest,
                runner,
                event_id=event_id,
                model=model,
            )
            calls_used += used
            found.extend(classified)

    if not dry_run:
        for asset in found:
            catalogue.put_ir_asset(asset)
        if new_seen:
            merged_seen = (seen_list + new_seen)[-_MAX_SEEN_URLS:]
            updated_state = dict(prev_state)
            updated_state["ir_seen_urls"] = merged_seen
            catalogue.set_state("fetch_state", source.key, updated_state)

    return found, calls_used
