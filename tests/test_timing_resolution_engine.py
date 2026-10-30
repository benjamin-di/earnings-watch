from datetime import datetime, timezone
from earnings_watch.engine import verify as verify

"Timing decisions at the evidence → reconciliation → delivery boundary."
NOW = datetime(2026, 9, 6, tzinfo=timezone.utc)
