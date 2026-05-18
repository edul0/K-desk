import json
import os
from datetime import datetime

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def _service():
    creds_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if not creds_json:
        raise RuntimeError("Missing GOOGLE_SERVICE_ACCOUNT_JSON")
    creds_info = json.loads(creds_json)
    creds = Credentials.from_service_account_info(creds_info, scopes=SCOPES)
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def _next_ticket_id() -> str:
    # ID temporal para evitar leitura completa da planilha a cada chamada
    return f"TKT-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"


def append_ticket(
    requester_name: str,
    requester_email: str,
    description: str,
    kb_article_id: str,
    service: str,
    category: str,
    priority: str,
    estimated_resolution_time: str,
    escalation_required: bool,
    escalation_reason: str,
    collected_fields: dict,
) -> str:
    spreadsheet_id = os.environ.get("TICKETS_SPREADSHEET_ID", "")
    sheet_name = os.environ.get("TICKETS_SHEET_NAME", "tickets")
    if not spreadsheet_id:
        raise RuntimeError("Missing TICKETS_SPREADSHEET_ID")

    svc = _service()
    ticket_id = _next_ticket_id()
    now = datetime.utcnow().isoformat(timespec="seconds") + "Z"

    row = [
        ticket_id,
        now,
        requester_name,
        requester_email,
        description,
        kb_article_id,
        service,
        category,
        priority,
        estimated_resolution_time,
        "sim" if escalation_required else "não",
        escalation_reason,
        json.dumps(collected_fields, ensure_ascii=False),
        "Aberto",
    ]

    svc.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id,
        range=f"{sheet_name}!A:N",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": [row]},
    ).execute()

    return ticket_id
