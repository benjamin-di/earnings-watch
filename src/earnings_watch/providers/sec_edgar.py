import json
from earnings_watch.model.sources import Source
from earnings_watch.transport import Transport


async def signals(transport: Transport, source: Source, contact: str) -> list[dict]:
    if not contact or "@" not in contact:
        raise ValueError("SEC provider requires a configured contact user agent")
    if not source.sec_cik:
        return []
    page = await transport.get(
        f"https://data.sec.gov/submissions/CIK{source.sec_cik:010d}.json",
        user_agent="earnings-watch " + contact,
    )
    recent = json.loads(page.body).get("filings", {}).get("recent", {})
    return [
        {"id": accession, "date": day, "form": form}
        for accession, day, form, items in zip(
            recent.get("accessionNumber", []),
            recent.get("filingDate", []),
            recent.get("form", []),
            recent.get("items", []),
        )
        if form == "6-K" or (form == "8-K" and "2.02" in items)
    ]
