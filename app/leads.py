"""Parse uploaded lead spreadsheets (.xlsx or .csv) into name/phone records."""

import csv
import io
import re
from dataclasses import dataclass, field

from openpyxl import load_workbook

NAME_HEADERS = ("name", "customer", "lead", "contact name", "full name")
PHONE_HEADERS = ("phone", "mobile", "number", "contact", "cell", "whatsapp", "tel")
# A heading with one of these words is about something else, e.g. "Company Name", "Lead Source", "Account Number".
NOT_A_PERSON = ("company", "business", "firm", "shop", "store", "organisation", "organization", "brand", "source",
                "status", "id", "father", "product", "city", "area", "address", "email")


class LeadFileError(ValueError):
    pass


RESERVED_KEYS = {"customer_name", "business_name", "phone", "name"}


@dataclass
class ParseResult:
    leads: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    # Extra columns besides name/phone, passed to the voice agent: [{"key": "city", "label": "City"}]
    detail_fields: list[dict] = field(default_factory=list)


def _field_key(label: str, used: set[str]) -> str:
    """Turn a column header into a variable name Bolna can use in the prompt, e.g. 'Last Purchase' -> last_purchase."""
    key = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")[:40] or "field"
    if key[0].isdigit() or key in RESERVED_KEYS:
        key = "lead_" + key
    base, n = key, 2
    while key in used:
        key, n = f"{base}_{n}", n + 1
    used.add(key)
    return key


def _cell_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value).strip()[:500]


def normalize_phone(raw, default_country_code: str) -> str | None:
    """Return the number in E.164 format (+<digits>) or None if it is not a plausible phone number."""
    if raw is None:
        return None
    if isinstance(raw, float) and raw.is_integer():
        raw = int(raw)
    text = str(raw).strip()
    if not text:
        return None
    has_plus = text.startswith("+")
    digits = re.sub(r"\D", "", text)
    if not has_plus:
        if digits.startswith("00"):
            digits = digits[2:]
        elif len(digits) == 11 and digits.startswith("0"):
            digits = default_country_code + digits[1:]
        elif len(digits) == 10:
            digits = default_country_code + digits
    if not 8 <= len(digits) <= 15:
        return None
    return "+" + digits


def _rows_from_xlsx(content: bytes) -> list[list]:
    try:
        wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except Exception as exc:
        raise LeadFileError("Could not read the Excel file. Please upload a valid .xlsx file.") from exc
    ws = wb.worksheets[0]
    rows = [list(r) for r in ws.iter_rows(values_only=True)]
    wb.close()
    return rows


def _rows_from_csv(content: bytes) -> list[list]:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("latin-1")
    return [row for row in csv.reader(io.StringIO(text))]


def _find_column(header: list[str], candidates: tuple[str, ...], skip: tuple[str, ...] = ()) -> list[int]:
    """Columns whose heading matches, in column order: exact headings ("Phone") first, then partial ones."""
    exact = [i for i, cell in enumerate(header) if cell in candidates]
    partial = [i for i, cell in enumerate(header) if i not in exact and any(c in cell for c in candidates)
               and not set(re.findall(r"[a-z]+", cell)) & set(skip)]
    return exact + partial


def _pick_phone_column(columns: list[int], rows: list[list], default_country_code: str) -> int | None:
    """Of the columns headed like a phone number, the one whose cells most often are phone numbers."""
    sample = rows[:50]

    def phones(i: int) -> int:
        return sum(1 for r in sample if i < len(r) and normalize_phone(r[i], default_country_code))
    best = max(columns, key=lambda i: (phones(i), -columns.index(i)), default=None)
    return best if best is None or phones(best) else (columns[0] if columns else None)


def parse_leads(filename: str, content: bytes, default_country_code: str, max_leads: int) -> ParseResult:
    name = (filename or "").lower()
    if name.endswith(".xlsx"):
        rows = _rows_from_xlsx(content)
    elif name.endswith(".csv"):
        rows = _rows_from_csv(content)
    else:
        raise LeadFileError("Unsupported file type. Please upload an .xlsx (or .csv) file.")

    rows = [r for r in rows if any(c not in (None, "") for c in r)]
    if not rows:
        raise LeadFileError("The file is empty.")

    raw_header = [str(c or "").strip() for c in rows[0]]
    header = [h.lower() for h in raw_header]
    phone_col = _pick_phone_column(_find_column(header, PHONE_HEADERS), rows[1:], default_country_code)
    name_col = next(iter(_find_column([h if i != phone_col else "" for i, h in enumerate(header)], NAME_HEADERS,
                                      skip=NOT_A_PERSON)), None)
    if phone_col is not None:
        data_rows, first_row_number = rows[1:], 2
        if name_col is None:
            name_col = 0 if phone_col != 0 else 1
    else:
        # No recognisable header: assume column A = name, column B = phone.
        name_col, phone_col = 0, 1
        if normalize_phone(rows[0][1] if len(rows[0]) > 1 else None, default_country_code):
            data_rows, first_row_number = rows, 1
        else:
            data_rows, first_row_number = rows[1:], 2

    result = ParseResult()
    detail_cols: list[tuple[int, str]] = []
    if first_row_number == 2:  # the file has a header row: every other named column becomes a lead detail
        used: set[str] = set()
        for i, label in enumerate(raw_header):
            if i not in (name_col, phone_col) and label:
                key = _field_key(label, used)
                detail_cols.append((i, key))
                result.detail_fields.append({"key": key, "label": label[:100]})

    seen: set[str] = set()
    for offset, row in enumerate(data_rows):
        row_number = first_row_number + offset
        raw_name = row[name_col] if name_col < len(row) else None
        raw_phone = row[phone_col] if phone_col < len(row) else None
        phone = normalize_phone(raw_phone, default_country_code)
        if not phone:
            result.errors.append({"row": row_number, "reason": f"Invalid phone number: {raw_phone!r}"})
            continue
        if phone in seen:
            result.errors.append({"row": row_number, "reason": f"Duplicate phone number: {phone}"})
            continue
        seen.add(phone)
        lead_name = str(raw_name).strip() if raw_name not in (None, "") else "there"
        details = {key: _cell_text(row[i] if i < len(row) else None) for i, key in detail_cols}
        result.leads.append({"row_number": row_number, "name": lead_name[:200], "phone": phone, "details": details})

    if not result.leads:
        raise LeadFileError("No valid leads found. The file needs a name column and a phone number column.")
    if len(result.leads) > max_leads:
        raise LeadFileError(f"Too many leads ({len(result.leads)}). The maximum per campaign is {max_leads}.")
    return result
