import io

import pytest
from openpyxl import Workbook

from app.leads import LeadFileError, normalize_phone, parse_leads


def xlsx(rows):
    wb = Workbook()
    for r in rows:
        wb.active.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


@pytest.mark.parametrize("raw,expected", [
    ("9876543210", "+919876543210"),
    (9876543210, "+919876543210"),
    (9876543210.0, "+919876543210"),
    ("+1 (415) 555-0100", "+14155550100"),
    ("0044 20 7946 0958", "+442079460958"),
    ("09876543210", "+919876543210"),
    ("919876543210", "+919876543210"),
    ("12345", None),
    ("", None),
    (None, None),
])
def test_normalize_phone(raw, expected):
    assert normalize_phone(raw, "91") == expected


def test_parse_with_headers_any_order():
    content = xlsx([["Mobile Number", "Customer Name"], ["9876543210", "Asha"], ["bad", "Ravi"], ["9876543210", "Dup"]])
    result = parse_leads("leads.xlsx", content, "91", 100)
    assert result.leads == [{"row_number": 2, "name": "Asha", "phone": "+919876543210", "details": {}}]
    assert [e["row"] for e in result.errors] == [3, 4]


def test_parse_without_headers():
    result = parse_leads("leads.xlsx", xlsx([["Asha", 9876543210], ["Ravi", 9876500000]]), "91", 100)
    assert [l["name"] for l in result.leads] == ["Asha", "Ravi"]


def test_parse_csv():
    result = parse_leads("leads.csv", b"name,phone\nAsha,+14155550100\n", "91", 100)
    assert result.leads[0]["phone"] == "+14155550100"


def test_rejects_too_many_and_bad_files():
    with pytest.raises(LeadFileError, match="Too many"):
        parse_leads("l.csv", b"name,phone\na,9876543210\nb,9876543211\n", "91", 1)
    with pytest.raises(LeadFileError, match="Unsupported"):
        parse_leads("l.pdf", b"x", "91", 10)
    with pytest.raises(LeadFileError, match="valid .xlsx"):
        parse_leads("l.xlsx", b"not a zip", "91", 10)
    with pytest.raises(LeadFileError, match="No valid leads"):
        parse_leads("l.csv", b"name,phone\na,123\n", "91", 10)


def test_extra_columns_become_details():
    content = xlsx([["Name", "Phone", "Company", "Last Purchase", "Last Purchase", "2024 spend"],
                    ["Asha", 9876543210, "Acme", "Inverter", "Panels", 1200.0]])
    result = parse_leads("leads.xlsx", content, "91", 10)
    assert result.detail_fields == [{"key": "company", "label": "Company"},
                                    {"key": "last_purchase", "label": "Last Purchase"},
                                    {"key": "last_purchase_2", "label": "Last Purchase"},
                                    {"key": "lead_2024_spend", "label": "2024 spend"}]
    assert result.leads[0]["details"] == {"company": "Acme", "last_purchase": "Inverter",
                                          "last_purchase_2": "Panels", "lead_2024_spend": "1200"}


def test_columns_found_in_any_order_and_not_confused_by_similar_headings():
    from app.leads import parse_leads
    cases = {
        "Company Name,Name,Phone": "Name",
        "Phone,City,Name": "Name",
        "Lead Source,Customer Name,Account Number,Mobile": "Customer Name",
        "Company,Contact Name,Contact Number,Address": "Contact Name",
    }
    for header, name_heading in cases.items():
        cols = header.split(",")
        row = ["9876543210" if c in ("Phone", "Mobile", "Contact Number") else "12345" if c == "Account Number"
               else f"v-{c}" for c in cols]
        result = parse_leads("a.csv", f"{header}\n{','.join(row)}".encode(), "91", 10)
        lead = result.leads[0]
        assert lead["name"] == f"v-{name_heading}" and lead["phone"] == "+919876543210", header
        assert name_heading not in [f["label"] for f in result.detail_fields]
