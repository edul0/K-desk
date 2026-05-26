import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Dict

from ti_support_agent import (
    KB_PATH,
    TICKETS_PATH,
    classify_priority,
    estimate_eta,
    is_security_case,
    load_kb,
    normalize,
    register_ticket,
    score_article,
    split_csv_list,
)

HOST = "0.0.0.0"  # permite acesso por outros dispositivos na mesma rede Wi-Fi
PORT = 8080

ARTICLES = load_kb(Path(KB_PATH))


class SupportHandler(BaseHTTPRequestHandler):
    def _send_json(self, payload: Dict, status: int = 200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path != "/triage":
            self._send_json({"error": "Endpoint não encontrado"}, 404)
            return

        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8") if length else "{}"
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            self._send_json({"error": "JSON inválido"}, 400)
            return

        requester_name = (data.get("requester_name") or "Usuário não identificado").strip()
        requester_email = (data.get("requester_email") or "não informado").strip()
        description = (data.get("description") or "").strip()
        answers = data.get("answers") or {}

        if not description:
            self._send_json({"error": "Campo 'description' é obrigatório"}, 400)
            return

        ranked = sorted(
            ((a, score_article(description, a)) for a in ARTICLES), key=lambda x: x[1], reverse=True
        )
        best, best_score = ranked[0]
        second_score = ranked[1][1] if len(ranked) > 1 else 0.0
        ambiguous = best_score < 2.0 or (best_score - second_score) < 1.2

        missing_required = []
        combined = normalize(description + " " + " ".join(str(v) for v in answers.values()))
        for req in best.required_information[:5]:
            probe = normalize(req).split()
            if probe and not any(token in combined for token in probe):
                missing_required.append(req)

        if ambiguous and not answers:
            questions = split_csv_list(best.diagnostic_questions)[:4]
            self._send_json(
                {
                    "status": "need_more_info",
                    "kb_article_id": best.article_id,
                    "kb_article_title": best.title,
                    "questions": questions,
                    "message": "Preciso de mais detalhes para classificar corretamente.",
                }
            )
            return

        if missing_required:
            self._send_json(
                {
                    "status": "missing_required",
                    "required_fields": missing_required,
                    "message": "Antes do registro, preciso das informações obrigatórias.",
                }
            )
            return

        priority = classify_priority(description + " " + " ".join(str(v) for v in answers.values()), best.priority_guidance)
        eta = estimate_eta(priority, best.estimated_resolution_time)
        escalation = is_security_case(description, best) or priority == "Crítica"

        ticket_id = register_ticket(
            TICKETS_PATH,
            requester_name,
            requester_email,
            description,
            best,
            priority,
            eta,
            answers,
            escalation,
        )

        self._send_json(
            {
                "status": "registered",
                "ticket_id": ticket_id,
                "kb_article_id": best.article_id,
                "kb_article_title": best.title,
                "service": best.service,
                "category": best.category,
                "priority": priority,
                "estimated_resolution_time": eta,
                "escalation_required": escalation,
                "escalation_criteria": best.escalation_criteria if escalation else "",
                "resolution_steps": best.resolution_steps,
                "workaround": best.workaround,
            }
        )


def run_server():
    server = HTTPServer((HOST, PORT), SupportHandler)
    print(f"Servidor ativo em http://{HOST}:{PORT}")
    print("Acesse pelo IP da máquina no mesmo Wi-Fi, ex.: http://192.168.1.10:8080/triage")
    server.serve_forever()


if __name__ == "__main__":
    run_server()
