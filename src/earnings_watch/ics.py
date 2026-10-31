from datetime import datetime, timezone
from typing import Any

from .calendar import desired


def escape(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("\r", "")
        .replace("\n", "\\n")
        .replace(";", "\\;")
        .replace(",", "\\,")
    )


def render(events: list[dict[str, Any]]) -> str:
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Earnings Watch//EN", "CALSCALE:GREGORIAN"]
    for entry in desired(events):
        lines.extend(
            [
                "BEGIN:VEVENT",
                "UID:" + entry["key"],
                "SEQUENCE:" + str(entry["revision"]),
                "DTSTAMP:" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
                "TRANSP:TRANSPARENT",
                "SUMMARY:" + escape(entry["title"]),
                "DESCRIPTION:" + escape(entry["body"]),
            ]
        )
        for field in ("start", "end"):
            prefix = "DTSTART" if field == "start" else "DTEND"
            if entry["all_day"]:
                lines.append(prefix + ";VALUE=DATE:" + entry[field].replace("-", ""))
            else:
                stamp = datetime.fromisoformat(entry[field]).astimezone(timezone.utc)
                lines.append(prefix + ":" + stamp.strftime("%Y%m%dT%H%M%SZ"))
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    # RFC 5545 folds at 75 octets without splitting UTF-8 characters.
    folded = []
    for line in lines:
        row = ""
        for char in line:
            if len((row + char).encode()) > 75:
                folded.append(row)
                row = " "
            row += char
        folded.append(row)
    return "\r\n".join(folded) + "\r\n"
