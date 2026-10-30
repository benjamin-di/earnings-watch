from html.parser import HTMLParser
from urllib.parse import urljoin

import html2text

from earnings_watch.model.urls import public_url
from earnings_watch.transport import Transport
from .base import Capture


class Links(HTMLParser):
    def __init__(self, url: str):
        super().__init__(convert_charrefs=True)
        self.url = url
        self.links: list[dict[str, str]] = []
        self.current: str | None = None
        self.text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.current = dict(attrs).get("href")
            self.text = []

    def handle_data(self, data):
        if self.current:
            self.text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self.current:
            try:
                self.links.append(
                    {
                        "url": public_url(urljoin(self.url, self.current)),
                        "title": " ".join(self.text).strip()[:500],
                    }
                )
            except ValueError:
                pass
            self.current = None


async def capture(transport: Transport, url: str) -> Capture:
    response = await transport.get(url)
    if response.status == 304:
        return Capture(response.url, "")
    if "html" not in response.content_type and not response.content_type.startswith("text/"):
        raise ValueError("calendar page is not text or HTML")
    body = response.body.decode("utf-8", errors="replace")
    parser = Links(response.url)
    parser.feed(body)
    converter = html2text.HTML2Text()
    converter.ignore_images, converter.body_width = True, 0
    text = converter.handle(body)
    if len(text) > 150000:
        raise ValueError("calendar exceeds the extraction budget")
    return Capture(response.url, text, tuple(parser.links[:1000]))
