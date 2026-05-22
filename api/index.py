import csv
import io
import json
import os
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from flask import Flask, Response, jsonify, request

from agent_core import load_kb, triage
from vercel_sql_store import append_ticket, init_db, list_tickets, update_ticket_status

app = Flask(__name__)

DEFAULT_KB_FILE = Path(__file__).parent.parent / "data" / "support_knowledge_base.csv"
KB_FILE = Path(os.environ.get("KB_CSV_PATH", str(DEFAULT_KB_FILE)))
ARTICLES = load_kb(KB_FILE)


def gemini_assist(prompt: str) -> str | None:
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        return None
    try:
        model = os.environ.get("GEMINI_MODEL", "gemini-1.5-flash")
        endpoint = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:generateContent?key={urllib.parse.quote(api_key)}"
        )
        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": prompt}],
                }
            ]
        }
        req = urllib.request.Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        candidates = data.get("candidates") or []
        if not candidates:
            return None
        parts = (candidates[0].get("content") or {}).get("parts") or []
        text = "".join((p.get("text") or "") for p in parts).strip()
        return text or None
    except Exception:
        return None


@app.route("/", methods=["GET"])
def home():
    from flask import redirect
    return redirect("/chat")


@app.route("/api/triage", methods=["POST"])
def triage_route():
    data = request.get_json(silent=True)
    if not data:
        raw = request.get_data(cache=False, as_text=True) or "{}"
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = {}

    requester_name = (data.get("requester_name") or "Usuário não identificado").strip()
    requester_email = (data.get("requester_email") or "não informado").strip()
    description = (data.get("description") or "").strip()
    answers = data.get("answers") or {}

    if not description:
        return jsonify({"error": "Campo 'description' é obrigatório"}), 400

    status, payload = triage(description, answers, ARTICLES)

    chat_context = data.get("chat_context") or []
    context_text = " | ".join(str(x) for x in chat_context[-8:])

    if status in {"need_more_info", "missing_required"}:
        pending_q = ""
        if isinstance(payload.get("questions"), list) and payload.get("questions"):
            pending_q = str(payload.get("questions")[0])
        ai_hint = gemini_assist(
            "Você é um atendente de TI experiente. Responda como humano, com empatia e objetividade. Faça uma pergunta curta para qualificar o chamado: "
            + " Contexto: " + context_text
            + " | Mensagem atual: " + description
            + " | Proxima pergunta obrigatoria: " + pending_q
        )
        result = {"status": status, **payload}
        if ai_hint:
            result["ai_message"] = ai_hint
        return jsonify(result), 200

    article = payload["article"]
    priority = payload["priority"]
    eta = payload.get("eta", "N/A")
    escalation = payload.get("escalation", False)

    try:
        ticket_id = append_ticket(
            requester_name=requester_name,
            requester_email=requester_email,
            description=description,
            kb_article_id=article.article_id,
            service=article.service,
            category=article.category,
            priority=priority,
            estimated_resolution_time=eta,
            escalation_required=escalation,
            escalation_reason=article.escalation_criteria if escalation else "",
            collected_fields=answers,
        )
    except Exception as e:
        ticket_id = f"TKT-{uuid.uuid4().hex[:8].upper()}"
        app.logger.warning(f"Vercel SQL indisponível: {e}. Ticket fallback: {ticket_id}")

    return jsonify({
        "status": "registered",
        "ticket_id": ticket_id,
        "kb_article_id": article.article_id,
        "kb_article_title": article.title,
        "service": article.service,
        "category": article.category,
        "priority": priority,
        "estimated_resolution_time": eta,
        "escalation_required": escalation,
        "escalation_criteria": article.escalation_criteria if escalation else "",
        "resolution_steps": article.resolution_steps,
        "workaround": article.workaround,
    }), 200


@app.route("/api/chat", methods=["POST"])
def chat_proxy():
    """Compatibilidade do chat sem n8n: usa triagem local + SQL. Agora com suporte nativo ao Make"""
    import requests
    data = request.get_json(silent=True) or {}

    make_webhook = os.environ.get("MAKE_WEBHOOK_URL", "").strip()
    if make_webhook:
        try:
            r = requests.post(make_webhook, json=data, timeout=15)
            if r.status_code == 200:
                return jsonify(r.json()), 200
            else:
                return jsonify({"error": "Erro no Make webhook", "details": r.text}), 502
        except Exception as e:
            return jsonify({"error": "Falha ao conectar no Make", "details": str(e)}), 500
    requester_name = (data.get("requester_name") or "Usuário não identificado").strip()
    requester_email = (data.get("requester_email") or "não informado").strip()
    description = (data.get("description") or "").strip()
    answers = data.get("answers") or {}

    if not description:
        return jsonify({"error": "Campo 'description' é obrigatório"}), 400

    status, payload = triage(description, answers, ARTICLES)
    chat_context = data.get("chat_context") or []
    context_text = " | ".join(str(x) for x in chat_context[-8:])

    if status in {"need_more_info", "missing_required"}:
        msg = payload.get("message") or "Preciso de mais detalhes para continuar o atendimento."
        pending_q = ""
        if isinstance(payload.get("questions"), list) and payload.get("questions"):
            pending_q = str(payload.get("questions")[0])
        ai_hint = gemini_assist(
            "Você é um atendente de TI experiente. Responda como humano, com empatia e objetividade. Faça uma pergunta curta para qualificar o chamado: "
            + description
            + " | Contexto: " + context_text
            + " | Proxima pergunta obrigatoria: " + pending_q
        )
        if ai_hint:
            msg = ai_hint
        return jsonify({"status": status, "ai_message": msg, **payload}), 200

    article = payload["article"]
    priority = payload["priority"]
    eta = payload.get("eta", "N/A")
    escalation = payload.get("escalation", False)

    try:
        ticket_id = append_ticket(
            requester_name=requester_name,
            requester_email=requester_email,
            description=description,
            kb_article_id=article.article_id,
            service=article.service,
            category=article.category,
            priority=priority,
            estimated_resolution_time=eta,
            escalation_required=escalation,
            escalation_reason=article.escalation_criteria if escalation else "",
            collected_fields=answers,
        )
    except Exception as e:
        ticket_id = f"TKT-{uuid.uuid4().hex[:8].upper()}"
        app.logger.warning(f"Vercel SQL indisponível: {e}. Ticket fallback: {ticket_id}")

    response = {
        "status": "registered",
        "ticket_id": ticket_id,
        "kb_article_id": article.article_id,
        "kb_article_title": article.title,
        "service": article.service,
        "category": article.category,
        "priority": priority,
        "estimated_resolution_time": eta,
        "escalation_required": escalation,
        "escalation_criteria": article.escalation_criteria if escalation else "",
        "resolution_steps": article.resolution_steps,
        "workaround": article.workaround,
    }
    ai_follow_up = gemini_assist(
        f"Chamado {ticket_id} registrado com prioridade {priority}. "
        f"Responda como um atendente humano de TI em português, de forma curta e útil."
    )
    if ai_follow_up:
        response["ai_message"] = ai_follow_up
    return jsonify(response), 200


@app.route("/api/tickets", methods=["GET"])
def tickets_route():
    status = (request.args.get("status") or "").strip()
    if status and status not in {"Aberto", "Em andamento", "Finalizado"}:
        return jsonify({"error": "Status inválido."}), 400
    try:
        rows = list_tickets(status=status or None, limit=300)
        for row in rows:
            dt = row.get("created_at")
            if dt is not None:
                row["created_at"] = dt.isoformat()
        return jsonify({"tickets": rows}), 200
    except Exception as e:
        app.logger.error(f"tickets_route error: {e}")
        return jsonify({"tickets": [], "error": "Falha ao carregar histórico."}), 200


@app.route("/api/tickets/<ticket_id>/status", methods=["POST"])
def ticket_status_route(ticket_id: str):
    data = request.get_json(silent=True) or {}
    status = (data.get("status") or "").strip()
    if status not in {"Aberto", "Em andamento", "Finalizado"}:
        return jsonify({"error": "Status inválido."}), 400
    ok = update_ticket_status(ticket_id=ticket_id, status=status)
    if not ok:
        return jsonify({"error": "Ticket não encontrado."}), 404
    return jsonify({"ok": True, "ticket_id": ticket_id, "status": status}), 200


@app.route("/api/tickets/export.csv", methods=["GET"])
def tickets_export_csv_route():
    rows = list_tickets(limit=1000)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "ticket_id",
            "created_at",
            "requester_name",
            "requester_email",
            "description",
            "service",
            "category",
            "priority",
            "estimated_resolution_time",
            "status",
            "escalation_required",
            "escalation_reason",
            "ai_note",
        ]
    )
    for row in rows:
        description = str(row.get("description") or "")
        ai_note = gemini_assist(
            "Resuma em uma linha o próximo passo para o chamado: " + description[:400]
        ) or ""
        writer.writerow(
            [
                row.get("ticket_id", ""),
                (row.get("created_at").isoformat() if row.get("created_at") else ""),
                row.get("requester_name", ""),
                row.get("requester_email", ""),
                description,
                row.get("service", ""),
                row.get("category", ""),
                row.get("priority", ""),
                row.get("estimated_resolution_time", ""),
                row.get("status", ""),
                row.get("escalation_required", ""),
                row.get("escalation_reason", ""),
                ai_note,
            ]
        )

    csv_data = output.getvalue()
    return Response(
        csv_data,
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=chamados_kdesk.csv"},
    )
@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"ok": True, "kb_articles": len(ARTICLES)}), 200


@app.route("/api/init-db", methods=["POST"])
def init_db_route():
    try:
        init_db()
        return jsonify({"ok": True, "message": "Tabela tickets validada/criada com sucesso."}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/chat", methods=["GET"])
def chat_interface():
    html = r"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>K-Desk · Suporte de TI</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Inter',sans-serif;background:#f4f6f9;display:flex;align-items:center;justify-content:center;min-height:100vh;padding:16px}
#app{width:440px;max-width:100%;display:flex;flex-direction:column;border-radius:16px;overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,0.10);background:#fff}
.kd-head{padding:14px 18px;background:#fff;border-bottom:1px solid #f0f2f5;display:flex;align-items:center;justify-content:space-between}
.kd-logo{width:34px;height:34px;border-radius:9px;background:#dbeafe;display:flex;align-items:center;justify-content:center}
.kd-logo svg{width:18px;height:18px;stroke:#1d4ed8;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.kd-brand{margin-left:10px}
.kd-brand-name{font-size:14px;font-weight:600;color:#111827}
.kd-brand-sub{font-size:11px;color:#6b7280;margin-top:1px}
.kd-online{display:flex;align-items:center;gap:5px;font-size:11px;color:#16a34a;font-family:'JetBrains Mono',monospace}
.kd-dot{width:6px;height:6px;border-radius:50%;background:#16a34a;animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
.kd-steps{display:flex;border-bottom:1px solid #f0f2f5;background:#fafafa}
.kd-step{flex:1;padding:10px 0;text-align:center;font-size:11px;color:#9ca3af;border-bottom:2px solid transparent;transition:all .2s;display:flex;align-items:center;justify-content:center;gap:5px;font-weight:500}
.kd-step svg{width:13px;height:13px;stroke:currentColor;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.kd-step.active{color:#1d4ed8;border-bottom-color:#1d4ed8}
.kd-step.done{color:#16a34a}
.screen{display:none;flex-direction:column;animation:fadeIn .2s ease}
.screen.on{display:flex}
@keyframes fadeIn{from{opacity:0;transform:translateY(5px)}to{opacity:1;transform:none}}
#s-welcome{padding:32px 28px 28px;align-items:center;gap:20px}
.welcome-icon{width:56px;height:56px;border-radius:16px;background:#dbeafe;display:flex;align-items:center;justify-content:center}
.welcome-icon svg{width:28px;height:28px;stroke:#1d4ed8;fill:none;stroke-width:1.75;stroke-linecap:round;stroke-linejoin:round}
.welcome-title{font-size:17px;font-weight:600;color:#111827;text-align:center}
.welcome-sub{font-size:13px;color:#6b7280;text-align:center;line-height:1.65;max-width:320px}
.form-fields{width:100%;display:flex;flex-direction:column;gap:14px}
.form-group{display:flex;flex-direction:column;gap:5px}
.form-label{font-size:11px;color:#6b7280;text-transform:uppercase;letter-spacing:.6px;font-weight:500}
.form-input{width:100%;padding:10px 13px;border:1px solid #e5e7eb;border-radius:8px;font-size:13.5px;font-family:'Inter',sans-serif;color:#111827;outline:none;transition:border-color .2s,box-shadow .2s;background:#fff}
.form-input:focus{border-color:#1d4ed8;box-shadow:0 0 0 3px rgba(29,78,216,.08)}
.form-input::placeholder{color:#9ca3af}
.btn-primary{width:100%;padding:11px;background:#1d4ed8;border:none;border-radius:9px;color:#fff;font-size:13.5px;font-weight:600;font-family:'Inter',sans-serif;cursor:pointer;transition:background .2s,transform .1s;display:flex;align-items:center;justify-content:center;gap:7px;margin-top:4px}
.btn-primary:hover{background:#1e40af}
.btn-primary:active{transform:scale(.99)}
.btn-primary:disabled{background:#9ca3af;cursor:not-allowed}
.btn-primary svg{width:15px;height:15px;stroke:#fff;fill:none;stroke-width:2.5;stroke-linecap:round;stroke-linejoin:round}
.userbar{padding:9px 16px;border-bottom:1px solid #f0f2f5;background:#fafafa;display:flex;align-items:center;gap:9px}
.avatar{width:28px;height:28px;border-radius:50%;background:#dbeafe;display:flex;align-items:center;justify-content:center;font-size:10px;font-weight:600;color:#1d4ed8;flex-shrink:0}
.userbar-name{font-size:12.5px;font-weight:500;color:#111827}
.userbar-email{font-size:11px;color:#6b7280;font-family:'JetBrains Mono',monospace}
#chat-box{flex:1;min-height:280px;max-height:320px;overflow-y:auto;padding:16px;display:flex;flex-direction:column;gap:10px;scrollbar-width:thin;scrollbar-color:#e5e7eb transparent}
.msg{display:flex;flex-direction:column;max-width:87%;animation:fadeIn .15s ease}
.msg.bot{align-self:flex-start}
.msg.usr{align-self:flex-end}
.bubble{padding:9px 13px;border-radius:12px;font-size:13.5px;line-height:1.55;white-space:pre-line}
.msg.bot .bubble{background:#f3f4f6;color:#111827;border-bottom-left-radius:3px}
.msg.usr .bubble{background:#1d4ed8;color:#fff;border-bottom-right-radius:3px}
.msg.typing .bubble{background:#f3f4f6;color:#9ca3af}
.msg-time{font-size:10px;color:#9ca3af;margin-top:3px;padding:0 3px;font-family:'JetBrains Mono',monospace}
.msg.usr .msg-time{text-align:right;color:#93c5fd}
.dots{display:inline-flex;gap:3px;align-items:center}
.dots span{width:5px;height:5px;border-radius:50%;background:#9ca3af;animation:dotpulse 1.2s infinite}
.dots span:nth-child(2){animation-delay:.2s}.dots span:nth-child(3){animation-delay:.4s}
@keyframes dotpulse{0%{transform:scale(.7);opacity:.5}40%{transform:scale(1);opacity:1}80%,100%{transform:scale(.7);opacity:.5}}
.input-row{padding:10px 13px;border-top:1px solid #f0f2f5;display:flex;gap:8px;background:#fff}
#chat-input{flex:1;padding:10px 13px;border:1px solid #e5e7eb;border-radius:8px;font-size:13.5px;font-family:'Inter',sans-serif;color:#111827;outline:none;transition:border-color .2s}
#chat-input:focus{border-color:#1d4ed8}
#chat-input::placeholder{color:#9ca3af}
#btn-send{padding:10px 16px;background:#1d4ed8;border:none;border-radius:8px;color:#fff;font-size:13px;font-weight:600;font-family:'Inter',sans-serif;cursor:pointer;transition:background .2s;white-space:nowrap}
#btn-send:hover{background:#1e40af}
#btn-send:disabled{background:#9ca3af;cursor:not-allowed}
#s-ticket{padding:22px 20px;gap:14px;overflow-y:auto}
.tick-header{display:flex;align-items:center;gap:12px}
.tick-icon{width:42px;height:42px;border-radius:11px;background:#dcfce7;display:flex;align-items:center;justify-content:center;flex-shrink:0}
.tick-icon svg{width:22px;height:22px;stroke:#16a34a;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.tick-title{font-size:15px;font-weight:600;color:#16a34a}
.tick-id{font-size:11px;color:#6b7280;font-family:'JetBrains Mono',monospace;margin-top:2px}
.divider{height:1px;background:#f0f2f5}
.tick-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.tick-field{background:#fafafa;border:1px solid #f0f2f5;border-radius:9px;padding:10px 12px;display:flex;flex-direction:column;gap:4px}
.tick-field.full{grid-column:1/-1}
.tick-label{font-size:10px;color:#9ca3af;text-transform:uppercase;letter-spacing:.7px;font-weight:500}
.tick-value{font-size:13px;color:#111827;font-weight:500;line-height:1.45}
.badge{display:inline-block;padding:3px 9px;border-radius:6px;font-size:11px;font-family:'JetBrains Mono',monospace;font-weight:500}
.badge-c{background:#fee2e2;color:#991b1b}
.badge-a{background:#fef3c7;color:#92400e}
.badge-m{background:#dbeafe;color:#1e3a8a}
.badge-b{background:#dcfce7;color:#14532d}
.esc-alert{background:#fef2f2;border:1px solid #fecaca;border-radius:9px;padding:10px 13px;font-size:12.5px;color:#b91c1c;display:flex;align-items:flex-start;gap:8px}
.esc-alert svg{width:15px;height:15px;stroke:currentColor;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round;flex-shrink:0;margin-top:1px}
.btn-new{width:100%;padding:10px;background:#fff;border:1px solid #e5e7eb;border-radius:9px;color:#6b7280;font-size:13px;font-family:'Inter',sans-serif;cursor:pointer;transition:border-color .2s,color .2s;margin-top:2px;display:flex;align-items:center;justify-content:center;gap:6px}
.btn-new:hover{border-color:#1d4ed8;color:#1d4ed8}
.btn-new svg{width:14px;height:14px;stroke:currentColor;fill:none;stroke-width:2.5;stroke-linecap:round;stroke-linejoin:round}
.kd-menu{display:flex;gap:12px;padding:12px 16px;border-bottom:1px solid #f0f2f5;background:#fff;align-items:center;justify-content:center;}
.kd-tabs-group{display:flex;gap:4px;}
.kd-tab{padding:8px 14px;border:none;border-radius:6px;background:transparent;color:#4b5563;font-size:13px;font-weight:400;cursor:pointer;transition:all .2s;white-space:nowrap;line-height:1.2;}
.kd-tab:hover{background:#f3f4f6;color:#111827}
.kd-tab.on{background:#e8f0fe;color:#1a73e8;}
.kd-admin-group{display:flex;gap:8px;}
.kd-btn-action{padding:8px 12px;border:1px solid #d2d5d6;border-radius:6px;background:#fff;color:#3c4043;font-size:13px;font-weight:500;cursor:pointer;display:flex;align-items:center;gap:6px;transition:all .2s;}
.kd-btn-action:hover{background:#f8f9fa}
.kd-btn-new{background:#2563eb;color:#fff;border:none;}
.kd-btn-new:hover{background:#1d4ed8;color:#fff}
.kd-history{display:none;max-height:240px;overflow-y:auto;padding:16px;background:#f8fafc;border-bottom:1px solid #f0f2f5}
.kd-history.on{display:flex;flex-direction:column;gap:10px}
.hist-card{padding:14px;border:1px solid #e2e8f0;background:#fff;border-radius:10px;box-shadow:0 1px 3px rgba(0,0,0,0.03);transition:transform .15s}
.hist-card:hover{transform:translateY(-1px);box-shadow:0 4px 6px -1px rgba(0,0,0,0.05)}
.hist-top{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}
.hist-id{font-family:'JetBrains Mono',monospace;font-size:11.5px;font-weight:600;color:#1d4ed8}
.hist-status{font-size:10px;text-transform:uppercase;letter-spacing:0.5px;font-weight:600;padding:3px 7px;border-radius:4px;background:#f1f5f9;color:#475569}
.hist-desc{font-size:13px;color:#334155;line-height:1.5;margin-bottom:10px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;}
.hist-meta{display:flex;justify-content:space-between;align-items:center;font-size:11px;color:#64748b}
.hist-actions{display:flex;gap:8px;margin-top:10px;padding-top:10px;border-top:1px dashed #e2e8f0}
.hist-btn{padding:5px 10px;border:1px solid #cbd5e1;border-radius:6px;background:#fff;color:#475569;font-size:11.5px;font-weight:500;cursor:pointer;transition:all .15s}
.hist-btn:hover{background:#f8fafc;color:#0f172a;border-color:#94a3b8}
.hist-btn-primary{background:#ecfdf5;color:#059669;border-color:#a7f3d0}
.hist-btn-primary:hover{background:#d1fae5;color:#047857}
</style>
</head>
<body>
<div id="app">
  <div class="kd-head">
    <div style="display:flex;align-items:center">
      <div class="kd-logo">
        <svg viewBox="0 0 24 24"><path d="M9 3H5a2 2 0 0 0-2 2v4m6-6h10a2 2 0 0 1 2 2v4M9 3v18m0 0h10a2 2 0 0 0 2-2V9M9 21H5a2 2 0 0 1-2-2V9m0 0h18"/></svg>
      </div>
      <div class="kd-brand">
        <div class="kd-brand-name">K-Desk</div>
        <div class="kd-brand-sub">Central de suporte de TI</div>
      </div>
    </div>
    <div class="kd-online"><div class="kd-dot"></div>online</div>
  </div>
  <div class="kd-steps">
    <div class="kd-step active" id="step-1">
      <svg viewBox="0 0 24 24"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>
      Identificação
    </div>
    <div class="kd-step" id="step-2">
      <svg viewBox="0 0 24 24"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>
      Atendimento
    </div>
    <div class="kd-step" id="step-3">
      <svg viewBox="0 0 24 24"><polyline points="20 6 9 17 4 12"/></svg>
      Chamado
    </div>
  </div>
  <div class="kd-menu">
    <div class="kd-tabs-group">
      <button class="kd-tab on" id="tab-historico" onclick="kdLoadTickets('')">Histórico</button>
      <button class="kd-tab" id="tab-andamento" onclick="kdLoadTickets('Em andamento')">Em<br>andamento</button>
      <button class="kd-tab" id="tab-finalizado" onclick="kdLoadTickets('Finalizado')">Finalizados</button>
    </div>
    <div class="kd-admin-group">
      <button class="kd-btn-action" onclick="window.location='/api/tickets/export.csv'">
        <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="7 10 12 15 17 10"></polyline><line x1="12" y1="15" x2="12" y2="3"></line></svg>
        CSV
      </button>
      <button class="kd-btn-action kd-btn-new" onclick="kdReset(); kdScreen('s-welcome')">
        <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2"><line x1="12" y1="5" x2="12" y2="19"></line><line x1="5" y1="12" x2="19" y2="12"></line></svg>
        Novo
      </button>
    </div>
  </div>
  <div class="kd-history on" id="kd-history"></div>
  <div class="screen on" id="s-welcome">
    <div class="welcome-icon">
      <svg viewBox="0 0 24 24"><rect x="2" y="3" width="20" height="14" rx="2"/><path d="M8 21h8M12 17v4"/></svg>
    </div>
    <div class="welcome-title">Bem-vindo ao suporte inteligente</div>
    <div class="welcome-sub">Preencha seus dados para iniciar. O agente vai conduzir o atendimento e registrar seu chamado automaticamente.</div>
    <div class="form-fields">
      <div class="form-group">
        <label class="form-label" for="inp-name">Nome completo</label>
        <input class="form-input" id="inp-name" type="text" placeholder="Seu nome" autocomplete="name">
      </div>
      <div class="form-group">
        <label class="form-label" for="inp-email">E-mail corporativo</label>
        <input class="form-input" id="inp-email" type="email" placeholder="voce@empresa.com" autocomplete="email">
      </div>
      <button class="btn-primary" onclick="kdStart()">
        Iniciar atendimento
        <svg viewBox="0 0 24 24"><line x1="5" y1="12" x2="19" y2="12"/><polyline points="12 5 19 12 12 19"/></svg>
      </button>
    </div>
  </div>
  <div class="screen" id="s-chat">
    <div class="userbar">
      <div class="avatar" id="kd-initials">—</div>
      <div>
        <div class="userbar-name" id="kd-uname">—</div>
        <div class="userbar-email" id="kd-uemail">—</div>
      </div>
    </div>
    <div id="chat-box"></div>
    <div class="input-row">
      <input id="chat-input" type="text" placeholder="Descreva seu problema de TI..." onkeydown="if(event.key==='Enter')kdSend()">
      <button id="btn-send" onclick="kdSend()">Enviar</button>
    </div>
  </div>
  <div class="screen" id="s-ticket">
    <div class="tick-header">
      <div class="tick-icon">
        <svg viewBox="0 0 24 24"><polyline points="20 6 9 17 4 12"/></svg>
      </div>
      <div>
        <div class="tick-title">Chamado registrado com sucesso</div>
        <div class="tick-id" id="kt-id">—</div>
      </div>
    </div>
    <div class="divider"></div>
    <div class="tick-grid">
      <div class="tick-field"><span class="tick-label">Serviço</span><span class="tick-value" id="kt-svc">—</span></div>
      <div class="tick-field"><span class="tick-label">Categoria</span><span class="tick-value" id="kt-cat">—</span></div>
      <div class="tick-field"><span class="tick-label">Prioridade</span><span class="tick-value" id="kt-pri">—</span></div>
      <div class="tick-field"><span class="tick-label">Prazo estimado</span><span class="tick-value" id="kt-eta">—</span></div>
      <div class="tick-field full"><span class="tick-label">Artigo de referência</span><span class="tick-value" id="kt-art">—</span></div>
      <div class="tick-field full"><span class="tick-label">Próximos passos</span><span class="tick-value" id="kt-steps">—</span></div>
      <div class="tick-field full" id="kt-wblock" style="display:none"><span class="tick-label">Contorno disponível</span><span class="tick-value" id="kt-wk">—</span></div>
    </div>
    <div class="esc-alert" id="kt-esc" style="display:none">
      <svg viewBox="0 0 24 24"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>
      Chamado encaminhado para analista humano conforme critérios de escalonamento.
    </div>
    <button class="btn-new" onclick="kdReset()">
      <svg viewBox="0 0 24 24"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
      Abrir novo chamado
    </button>
  </div>
</div>
<script>
// Chama a Vercel como proxy — sem CORS
const API = "/api/chat";
let uName="",uEmail="",desc="",ans={},state="DESC",curQ="",chatContext=[];
let currentTicketFilter="";

function kdSelectTab(status){
  const tabs={"":"tab-historico","Em andamento":"tab-andamento","Finalizado":"tab-finalizado"};
  document.querySelectorAll('.kd-tab').forEach(t=>t.classList.remove('on'));
  const id=tabs[status]||"tab-historico";
  const el=document.getElementById(id);if(el)el.classList.add('on');
}

async function kdUpdateTicketStatus(ticketId,status){
  await fetch('/api/tickets/'+encodeURIComponent(ticketId)+'/status',{
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({status})
  });
  kdLoadTickets(currentTicketFilter);
}

async function kdLoadTickets(status){
  currentTicketFilter=status||"";
  kdSelectTab(currentTicketFilter);
  const box=document.getElementById('kd-history');
  box.innerHTML='Carregando chamados...';
  try{
    const q=currentTicketFilter?('?status='+encodeURIComponent(currentTicketFilter)):'';
    const res=await fetch('/api/tickets'+q);
    if(!res.ok){
      const txt=await res.text();
      box.innerHTML='Falha ao carregar histórico. '+txt.slice(0,120);
      return;
    }
    const data=await res.json();
    const tickets=data.tickets||[];
    if(!tickets.length){box.innerHTML='Nenhum chamado nesta visão.';return;}
    box.innerHTML=tickets.map(t=>{
      const openBtn=t.status==='Aberto'?`<button class="hist-btn hist-btn-primary" onclick="kdUpdateTicketStatus('${t.ticket_id}','Em andamento')">Assumir</button>`:'';
      const doneBtn=t.status!=='Finalizado'?`<button class="hist-btn" onclick="kdUpdateTicketStatus('${t.ticket_id}','Finalizado')">Concluir</button>`:'';
      const actionsHtml = (openBtn || doneBtn) ? `<div class="hist-actions">${openBtn}${doneBtn}</div>` : '';
      return `<div class="hist-card">
        <div class="hist-top">
          <span class="hist-id">${t.ticket_id}</span>
          <span class="hist-status">${t.status||'Aberto'}</span>
        </div>
        <div class="hist-desc">${(t.description||'').slice(0,160)}...</div>
        <div class="hist-meta">
          <span><strong>Pri:</strong> ${t.priority||'-'}</span>
          <span>${(t.created_at||'').replace('T',' ').slice(0,16)}</span>
        </div>
        ${actionsHtml}
      </div>`;
    }).join('');
  }catch(e){
    box.innerHTML='Falha ao carregar histórico.';
  }
}

function kdScreen(id){document.querySelectorAll('.screen').forEach(s=>s.classList.remove('on'));document.getElementById(id).classList.add('on')}
function kdStep(n){[1,2,3].forEach(i=>{const e=document.getElementById('step-'+i);e.classList.remove('active','done');if(i<n)e.classList.add('done');else if(i===n)e.classList.add('active')})}
function kdTime(){return new Date().toLocaleTimeString('pt-BR',{hour:'2-digit',minute:'2-digit'})}

function kdMsg(type,text){
  const box=document.getElementById('chat-box');
  const d=document.createElement('div');d.className='msg '+type;
  const b=document.createElement('div');b.className='bubble';b.textContent=text;
  const m=document.createElement('div');m.className='msg-time';m.textContent=kdTime();
  d.appendChild(b);d.appendChild(m);box.appendChild(d);box.scrollTop=box.scrollHeight;
  chatContext.push((type==='usr'?'Usuário: ':'Agente: ')+String(text||''));
  if(chatContext.length>20)chatContext=chatContext.slice(-20);
  return d;
}
function kdTyping(){
  const box=document.getElementById('chat-box');
  const d=document.createElement('div');d.className='msg typing';d.id='kd-typing';
  d.innerHTML='<div class="bubble"><span class="dots"><span></span><span></span><span></span></span></div>';
  box.appendChild(d);box.scrollTop=box.scrollHeight;
}
function kdRmTyping(){const e=document.getElementById('kd-typing');if(e)e.remove()}

function kdStart(){
  const n=document.getElementById('inp-name').value.trim();
  const e=document.getElementById('inp-email').value.trim();
  if(!n||!e){alert('Preencha nome e e-mail para continuar.');return}
  uName=n;uEmail=e;
  const ini=n.split(' ').map(w=>w[0]).slice(0,2).join('').toUpperCase();
  document.getElementById('kd-initials').textContent=ini;
  document.getElementById('kd-uname').textContent=n;
  document.getElementById('kd-uemail').textContent=e;
  kdStep(2);kdScreen('s-chat');state="DESC";
  kdMsg('bot','Ola, '+n+'! Descreva seu problema ou incidente de TI com o maximo de detalhes.');
  document.getElementById('chat-input').focus();
}

async function kdSend(){
  const inp=document.getElementById('chat-input');
  const btn=document.getElementById('btn-send');
  let text=inp.value.trim();
  if(!text)return;
  kdMsg('usr',text);
  inp.value='';
  btn.disabled=true;
  if(state==="DESC")desc=text;
  else if(state==="COLLECT"&&curQ)ans[curQ]=text;

  kdTyping();
  try{
    const r=await fetch(API,{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({description:desc,answers:ans,requester_name:uName,requester_email:uEmail,chat_context:chatContext})
    });
    const d=await r.json();
    kdRmTyping();

    if(d.status==="need_more_info"||d.status==="missing_required"){
      state="COLLECT";
      const arr=d.questions||d.required_fields||[];
      curQ=arr.find(q=>!ans[q])||arr[0];
      const botText=d.ai_message||d.message||('Preciso de mais informacoes:\n\n-> '+curQ);
      kdMsg('bot',botText);

	    }else if(d.status==="registered"){
      kdStep(3);
      document.getElementById('kt-id').textContent='Ticket: '+d.ticket_id;
      document.getElementById('kt-svc').textContent=d.service||'—';
      document.getElementById('kt-cat').textContent=d.category||'—';
      document.getElementById('kt-eta').textContent=d.estimated_resolution_time||'—';
      document.getElementById('kt-art').textContent=(d.kb_article_id||'')+'  '+(d.kb_article_title||'');
      document.getElementById('kt-steps').textContent=d.resolution_steps||'—';
      const p=(d.priority||'media').toLowerCase().normalize('NFD').replace(/[\u0300-\u036f]/g,'');
      const bm={critica:'badge-c',alta:'badge-a',media:'badge-m',baixa:'badge-b'};
      document.getElementById('kt-pri').innerHTML='<span class="badge '+(bm[p]||'badge-m')+'">'+d.priority+'</span>';
      if(d.workaround){document.getElementById('kt-wk').textContent=d.workaround;document.getElementById('kt-wblock').style.display='flex'}
	      if(d.escalation_required)document.getElementById('kt-esc').style.display='flex';
	      kdLoadTickets(currentTicketFilter);
	      kdScreen('s-ticket');
    }else{
      kdMsg('bot',d.ai_message||d.message||JSON.stringify(d));
    }
  }catch(e){
    kdRmTyping();
    kdMsg('bot','Erro de conexao. Tente novamente.');
    console.error(e);
  }
  btn.disabled=false;inp.focus()
}

function kdReset(){
  uName='';uEmail='';desc='';ans={};state='DESC';curQ='';
  document.getElementById('inp-name').value='';
  document.getElementById('inp-email').value='';
  document.getElementById('chat-box').innerHTML='';
  document.getElementById('kt-wblock').style.display='none';
  document.getElementById('kt-esc').style.display='none';
  document.getElementById('chat-input').disabled=false;
  document.getElementById('btn-send').disabled=false;
  kdStep(1);kdScreen('s-welcome');
}
document.addEventListener('keydown',function(e){
  if(e.key==='Enter'&&document.getElementById('s-welcome').classList.contains('on'))kdStart();
});
kdLoadTickets("");
</script>
</body>
</html>"""
    return html, 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
