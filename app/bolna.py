"""Thin client for the Bolna voice-agent API.

The voice agent (voice, languages, LLM, summary and outcome extraction) is set up once in Bolna's
dashboard, and its prompt uses {{variables}}. For every call this app sends the customer's business
details and the lead's details as user_data, and Bolna fills them into the prompt.

Bolna endpoints used:
  POST /call                       place one outbound call -> execution_id
  GET  /executions/{execution_id}  call status, transcript, summary, extracted data
"""

import httpx

from .config import Settings

# Execution statuses after which Bolna will not update the call any more.
TERMINAL_STATUSES = {
    "completed", "busy", "no-answer", "failed", "canceled", "cancelled",
    "stopped", "error", "balance-low", "call-disconnected-failed",
}


class BolnaError(RuntimeError):
    pass


def call_user_data(campaign: dict, lead: dict) -> dict:
    """Values for the {{variables}} in the dashboard agent's prompt (see README: Bolna agent setup)."""
    details = lead.get("details") or {}
    fields = campaign.get("detail_fields") or []
    welcome = (campaign.get("welcome_message") or "").strip() or (
        f"Hello {lead['name']}, I'm calling from {campaign['business_name']}. Do you have a minute?"
    )
    data = {
        "customer_name": lead["name"],
        "business_name": campaign["business_name"],
        "business_description": campaign.get("business_description") or "",
        "product_details": campaign["product_details"],
        "sales_instructions": campaign["sales_instructions"],
        "language": campaign.get("language") or "English",
        "welcome_message": welcome.replace("{customer_name}", lead["name"])
                                  .replace("{business_name}", campaign["business_name"]),
        # every extra Excel column as one readable line, e.g. "City: Chennai; Last Purchase: Inverter"
        "customer_details": "; ".join(f"{f['label']}: {details[f['key']]}" for f in fields
                                      if details.get(f["key"])) or "none",
    }
    for f in fields:  # each column also on its own, for prompts that want {{city}} etc.
        data.setdefault(f["key"], details.get(f["key"], ""))
    return data


class BolnaClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._http = httpx.Client(
            base_url=settings.bolna_api_base,
            headers={"Authorization": f"Bearer {settings.bolna_api_key}"},
            timeout=30,
        )

    def _request(self, method: str, path: str, **kwargs) -> dict:
        try:
            resp = self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise BolnaError(f"Bolna request failed: {exc}") from exc
        if resp.status_code >= 400:
            raise BolnaError(f"Bolna {method} {path} returned {resp.status_code}: {resp.text[:500]}")
        return resp.json() if resp.content else {}

    def start_call(self, phone: str, user_data: dict) -> str:
        body = {"agent_id": self.settings.bolna_agent_id, "recipient_phone_number": phone, "user_data": user_data}
        if self.settings.bolna_from_number:
            body["from_phone_number"] = self.settings.bolna_from_number
        data = self._request("POST", "/call", json=body)
        execution_id = data.get("execution_id") or data.get("call_id")
        if not execution_id:
            raise BolnaError(f"Bolna did not return an execution_id: {data}")
        return execution_id

    def get_execution(self, execution_id: str) -> dict:
        return self._request("GET", f"/executions/{execution_id}")


def make_bolna_client(settings: Settings):
    if not (settings.bolna_api_key and settings.bolna_agent_id):
        raise RuntimeError("BOLNA_API_KEY and BOLNA_AGENT_ID are required")
    return BolnaClient(settings)
