"""Build the downloadable results spreadsheet."""

import io
import json
import re

from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

LEAD_COLUMNS = [("Name", "name", 24), ("Phone", "phone", 18)]
RESULT_COLUMNS = [
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


def _format_extracted(value):
    if not value:
        return value
    try:
        return ", ".join(f"{k}: {v}" for k, v in json.loads(value).items())
    except (ValueError, AttributeError):
        return value


def build_results_workbook(campaign: dict, leads: list[dict]) -> bytes:
    """One row per lead: the uploaded columns (name, phone, extra details) followed by the call results."""
    detail_fields = campaign.get("detail_fields") or []
    headers = ([label for label, _, _ in LEAD_COLUMNS] + [f["label"] for f in detail_fields]
               + [label for label, _, _ in RESULT_COLUMNS])
    widths = [w for _, _, w in LEAD_COLUMNS] + [20] * len(detail_fields) + [w for _, _, w in RESULT_COLUMNS]

    wb = Workbook()
    ws = wb.active
    ws.title = "Results"
    ws.append([_safe(h) for h in headers])
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for i, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.freeze_panes = "A2"

    for lead in leads:
        details = lead.get("details") or {}
        row = [lead.get(key) for _, key, _ in LEAD_COLUMNS]
        row += [details.get(f["key"]) for f in detail_fields]
        row += [_format_extracted(lead.get(key)) if key == "extracted_data" else lead.get(key)
                for _, key, _ in RESULT_COLUMNS]
        ws.append([_safe(v) for v in row])

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
