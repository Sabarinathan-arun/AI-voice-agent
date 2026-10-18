"""Build the downloadable results spreadsheet."""

import io
import json
import re

from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

COLUMNS = [
    ("Name", "name", 24),
    ("Phone", "phone", 18),
    ("Call status", "status", 16),
    ("Outcome", "outcome", 20),
    ("Summary", "summary", 60),
    ("Duration (s)", "duration_seconds", 12),
    ("Called at (UTC)", "called_at", 22),
    ("Extracted data", "extracted_data", 40),
    ("Recording URL", "recording_url", 40),
    ("Transcript", "transcript", 80),
    ("Error", "error", 30),
]


def _safe(value):
    """Neutralise spreadsheet formula injection from call content."""
    if isinstance(value, str):
        if re.fullmatch(r"\+\d{8,15}", value):  # E.164 phone numbers are safe as-is
            return value
        if value[:1] in ("=", "+", "-", "@", "\t", "\r"):
            return "'" + value
        return value[:32000]
    return value


def build_results_workbook(campaign: dict, leads: list[dict]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Results"
    ws.append([c[0] for c in COLUMNS])
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for i, (_, _, width) in enumerate(COLUMNS, start=1):
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.freeze_panes = "A2"

    for lead in leads:
        row = []
        for _, key, _ in COLUMNS:
            value = lead.get(key)
            if key == "extracted_data" and value:
                try:
                    value = ", ".join(f"{k}: {v}" for k, v in json.loads(value).items())
                except (ValueError, AttributeError):
                    pass
            row.append(_safe(value))
        ws.append(row)

    info = wb.create_sheet("Campaign")
    for label, key in [("Business", "business_name"), ("Status", "status"), ("Started (UTC)", "started_at"),
                       ("Finished (UTC)", "finished_at"), ("Data deleted after (UTC)", "expires_at")]:
        info.append([label, _safe(campaign.get(key))])
    info.column_dimensions["A"].width = 26
    info.column_dimensions["B"].width = 40

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
