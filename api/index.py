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
    return gemini_autonomous_agent(prompt)


def gemini_autonomous_agent(prompt: str, system_instruction: str = "") -> str | None:
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
        if system_instruction:
            payload["system_instruction"] = {
                "parts": [{"text": system_instruction}]
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
    except Exception as e:
        app.logger.error(f"Erro no Gemini: {e}")
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
    """Compatibilidade do chat sem n8n: usa triagem local + SQL."""
    data = request.get_json(silent=True) or {}
    requester_name = (data.get("requester_name") or "Usuário não identificado").strip()
    requester_email = (data.get("requester_email") or "não informado").strip()
    description = (data.get("description") or "").strip()
    answers = data.get("answers") or {}

    if not description:
        return jsonify({"error": "Campo 'description' é obrigatório"}), 400

    if os.environ.get("GEMINI_API_KEY"):
        # Fluxo Autônomo com Gemini
        kb_data = []
        for a in ARTICLES:
            kb_data.append({
                "id": a.article_id,
                "title": a.title,
                "service": a.service,
                "category": a.category,
                "diagnostic_questions": a.diagnostic_questions,
                "priority_guidance": a.priority_guidance,
                "estimated_resolution_time": a.estimated_resolution_time,
                "resolution_steps": a.resolution_steps,
                "workaround": a.workaround,
                "escalation_criteria": a.escalation_criteria
            })
        
        chat_context = data.get("chat_context") or []
        context_text = "\n".join(str(x) for x in chat_context[-30:])

        system_prompt = f"""Você é um agente de suporte de TI (Nível 1).
Seu foco PRINCIPAL é tentar resolver o problema do usuário AQUI NO CHAT.
Abrir um chamado (ticket) é o ÚLTIMO RECURSO, e NUNCA deve ser a sua primeira resposta.

BASE DE CONHECIMENTO DISPONÍVEL (JSON):
{json.dumps(kb_data, ensure_ascii=False)}

FLUXO OBRIGATÓRIO (Siga exatamente esta ordem):
PASSO 1: Faça perguntas de diagnóstico curtas para entender o problema (se não estiver claro).
PASSO 2: Sugira UMA ação prática da base de conhecimento (ex: "Limpe o cache"). Peça para o usuário testar e aguarde.
PASSO 3: Se não resolver, tente outra sugestão, ou pergunte abertamente: "Você quer que eu abra um chamado para a equipe técnica?".
PASSO 4: SOMENTE se o usuário disser claramente "sim", "pode abrir", "quero", ou exigir o chamado, você avança para o registro.

COMO RESPONDER:
Você DEVE SEMPRE responder EXATAMENTE E APENAS com um bloco JSON. Não escreva texto solto.

Se você está nos Passos 1, 2 ou 3 (Investigando e perguntando):
```json
{{
  "thought": "O usuário relatou X. Vou sugerir Y e perguntar se resolveu.",
  "action": "reply",
  "message": "Sua pergunta ou dica de solução para o usuário"
}}
```

Se o usuário AUTORIZOU CLARAMENTE a abertura do chamado (Passo 4):
```json
{{
  "thought": "O usuário testou as dicas e não funcionou, e ele aceitou abrir o chamado. Vou registrar o ticket.",
  "action": "register_ticket",
  "ticket_data": {{
    "kb_article_id": "...",
    "kb_article_title": "...",
    "service": "...",
    "category": "...",
    "priority": "...",
    "estimated_resolution_time": "...",
    "resolution_steps": "...",
    "workaround": "...",
    "troubleshooting_summary": "Resumo de tudo que tentamos no chat...",
    "escalation_required": true,
    "escalation_criteria": "..."
  }}
}}
```

ATENÇÃO: Se o usuário ainda NÃO confirmou que deseja abrir o chamado, você É OBRIGADO a usar "action": "reply" e perguntar se ele quer.
"""

        prompt = f"Contexto da Conversa:\n{context_text}\n\nMensagem Atual do Usuário: {description}"

        ai_response = gemini_autonomous_agent(prompt, system_instruction=system_prompt)
        
        if ai_response:
            if "```json" in ai_response and '"action":' in ai_response:
                try:
                    start = ai_response.find("```json") + 7
                    end = ai_response.find("```", start)
                    json_str = ai_response[start:end].strip()
                    ticket_req = json.loads(json_str)
                    
                    if ticket_req.get("action") == "reply":
                        return jsonify({
                            "status": "need_more_info",
                            "ai_message": ticket_req.get("message", "Preciso de mais informações."),
                            "is_greeting": True
                        }), 200

                    if ticket_req.get("action") in ["register_ticket", "register"]:
                        t_data = ticket_req.get("ticket_data", {})
                        try:
                            # Sanitização para evitar TypeError/NotNullViolation se o Gemini retornar null
                            safe_str = lambda x: str(x) if x is not None else ""
                            
                            # Combina a descrição original com o resumo do troubleshooting e o histórico completo
                            final_description = (
                                f"Relato Inicial: {safe_str(description)}\n\n"
                                f"Resumo do Troubleshooting (IA): {safe_str(t_data.get('troubleshooting_summary'))}\n\n"
                                f"Histórico Completo do Chat:\n{safe_str(context_text)}"
                            )

                            ticket_id = append_ticket(
                                requester_name=safe_str(requester_name),
                                requester_email=safe_str(requester_email),
                                description=final_description,
                                kb_article_id=safe_str(t_data.get("kb_article_id")),
                                service=safe_str(t_data.get("service")),
                                category=safe_str(t_data.get("category")),
                                priority=safe_str(t_data.get("priority")) or "Média",
                                estimated_resolution_time=safe_str(t_data.get("estimated_resolution_time")),
                                escalation_required=bool(t_data.get("escalation_required")),
                                escalation_reason=safe_str(t_data.get("escalation_criteria")),
                                collected_fields={},
                            )
                        except Exception as e:
                            app.logger.error(f"Erro ao salvar no Vercel SQL: {e}")
                            ticket_id = f"TKT-ERRO-{uuid.uuid4().hex[:6].upper()}"
                            return jsonify({
                                "status": "registered",
                                "ticket_id": ticket_id,
                                "ai_message": f"⚠️ Concluí o diagnóstico, porém houve um erro ao salvar seu ticket no banco de dados: {str(e)}"
                            }), 200

                        return jsonify({
                            "status": "registered",
                            "ticket_id": ticket_id,
                            "kb_article_id": t_data.get("kb_article_id", ""),
                            "kb_article_title": t_data.get("kb_article_title", ""),
                            "service": t_data.get("service", ""),
                            "category": t_data.get("category", ""),
                            "priority": t_data.get("priority", "Média"),
                            "estimated_resolution_time": t_data.get("estimated_resolution_time", ""),
                            "escalation_required": t_data.get("escalation_required", False),
                            "escalation_criteria": t_data.get("escalation_criteria", ""),
                            "resolution_steps": t_data.get("resolution_steps", ""),
                            "workaround": t_data.get("workaround", ""),
                            "ai_message": "Tudo certo! Acabei de registrar o seu chamado com os detalhes que você me passou. Posso ajudar com mais alguma coisa?"
                        }), 200
                except Exception as e:
                    app.logger.error(f"Erro ao interpretar JSON autônomo do Gemini: {e}")
                    pass
            
            msg = ai_response.replace("```json", "").replace("```", "").strip()
            return jsonify({
                "status": "need_more_info", 
                "ai_message": msg,
                "is_greeting": True
            }), 200

    # Fallback: se não tiver API key, usa a triagem local legacy
    status, payload = triage(description, answers, ARTICLES)
    chat_context = data.get("chat_context") or []
    context_text = " | ".join(str(x) for x in chat_context[-8:])

    if status in {"need_more_info", "missing_required"}:
        msg = payload.get("message") or "Preciso de mais detalhes."
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
        return jsonify({"tickets": [], "error": f"Falha ao carregar histórico: {str(e)}"}), 200


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
    try:
        rows = list_tickets(limit=1000)
    except Exception as e:
        app.logger.error(f"export.csv list_tickets error: {e}")
        return Response("Erro ao carregar tickets: " + str(e), status=500, mimetype="text/plain")

    output = io.StringIO()
    writer = csv.writer(output, delimiter=';')
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
        ]
    )
    for row in rows:
        created_at = row.get("created_at")
        if created_at is None:
            created_at_str = ""
        elif hasattr(created_at, "isoformat"):
            created_at_str = created_at.isoformat()
        else:
            created_at_str = str(created_at)

        writer.writerow(
            [
                row.get("ticket_id", ""),
                created_at_str,
                row.get("requester_name", ""),
                row.get("requester_email", ""),
                str(row.get("description") or ""),
                row.get("service", ""),
                row.get("category", ""),
                row.get("priority", ""),
                row.get("estimated_resolution_time", ""),
                row.get("status", ""),
                row.get("escalation_required", ""),
                row.get("escalation_reason", ""),
            ]
        )

    csv_data = '\ufeff' + output.getvalue()
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


@app.route("/relatorio", methods=["GET"])
def relatorio():
    html = r"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>K-Desk · Relatório Analítico</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
:root {
  --bg: #060b14;
  --surface: #0d1526;
  --surface2: #111d35;
  --surface3: #162040;
  --border: #1e2d4a;
  --accent: #3b82f6;
  --accent2: #1d4ed8;
  --cyan: #06b6d4;
  --green: #10b981;
  --yellow: #f59e0b;
  --red: #ef4444;
  --purple: #8b5cf6;
  --text: #e2e8f0;
  --text-dim: #94a3b8;
  --text-muted: #475569;
  --mono: 'JetBrains Mono', monospace;
  --sans: 'Inter', sans-serif;
  --radius: 12px;
  --glow: 0 0 40px rgba(59,130,246,0.08);
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body {
  font-family: var(--sans);
  background: var(--bg);
  color: var(--text);
  min-height: 100vh;
  padding: 0;
}

/* Ambient background */
body::before {
  content: '';
  position: fixed;
  top: -30%;
  right: -20%;
  width: 70%;
  height: 80%;
  background: radial-gradient(ellipse, rgba(59,130,246,0.04) 0%, transparent 60%);
  pointer-events: none;
  z-index: 0;
}

/* ── TOP BAR ── */
.topbar {
  position: sticky;
  top: 0;
  z-index: 100;
  background: rgba(6,11,20,0.85);
  backdrop-filter: blur(16px);
  border-bottom: 1px solid var(--border);
  padding: 0 32px;
  height: 60px;
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.topbar-brand {
  display: flex;
  align-items: center;
  gap: 10px;
}
.topbar-logo {
  width: 30px;
  height: 30px;
  background: linear-gradient(135deg, var(--accent2), var(--cyan));
  border-radius: 8px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-family: var(--mono);
  font-size: 11px;
  font-weight: 500;
  color: #fff;
}
.topbar-title {
  font-size: 15px;
  font-weight: 600;
  color: var(--text);
}
.topbar-sub {
  font-size: 11px;
  font-family: var(--mono);
  color: var(--text-muted);
  letter-spacing: 1px;
  text-transform: uppercase;
}
.topbar-actions {
  display: flex;
  align-items: center;
  gap: 10px;
}
.btn-outline {
  padding: 7px 14px;
  background: transparent;
  border: 1px solid var(--border);
  border-radius: 8px;
  color: var(--text-dim);
  font-size: 12.5px;
  font-family: var(--sans);
  font-weight: 500;
  cursor: pointer;
  transition: all 0.2s;
  text-decoration: none;
  display: flex;
  align-items: center;
  gap: 6px;
}
.btn-outline:hover { border-color: var(--accent); color: var(--accent); }
.btn-accent {
  padding: 7px 14px;
  background: var(--accent2);
  border: none;
  border-radius: 8px;
  color: #fff;
  font-size: 12.5px;
  font-family: var(--sans);
  font-weight: 600;
  cursor: pointer;
  transition: all 0.2s;
  text-decoration: none;
  display: flex;
  align-items: center;
  gap: 6px;
}
.btn-accent:hover { background: var(--accent); }

/* ── LAYOUT ── */
.container {
  position: relative;
  z-index: 1;
  max-width: 1200px;
  margin: 0 auto;
  padding: 28px 24px 60px;
}

/* ── PAGE HEADER ── */
.page-header {
  margin-bottom: 28px;
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 12px;
}
.page-header h1 {
  font-size: 22px;
  font-weight: 700;
  color: var(--text);
}
.page-header p {
  font-size: 13px;
  color: var(--text-muted);
  margin-top: 4px;
}
.refresh-info {
  font-size: 11px;
  font-family: var(--mono);
  color: var(--text-muted);
  display: flex;
  align-items: center;
  gap: 6px;
}
.live-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--green);
  animation: pulse 2s infinite;
}
@keyframes pulse { 0%,100%{opacity:1;transform:scale(1)} 50%{opacity:0.4;transform:scale(0.8)} }

/* ── KPI CARDS ── */
.kpi-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 16px;
  margin-bottom: 28px;
}
.kpi-card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 20px;
  position: relative;
  overflow: hidden;
  animation: fadeUp 0.4s ease both;
  transition: transform 0.2s, box-shadow 0.2s;
}
.kpi-card:hover { transform: translateY(-2px); box-shadow: var(--glow); }
.kpi-card::before {
  content: '';
  position: absolute;
  top: 0; left: 0; right: 0;
  height: 2px;
}
.kpi-card.total::before { background: linear-gradient(90deg, var(--accent2), var(--cyan)); }
.kpi-card.aberto::before { background: linear-gradient(90deg, var(--yellow), #fb923c); }
.kpi-card.andamento::before { background: linear-gradient(90deg, var(--accent), var(--purple)); }
.kpi-card.finalizado::before { background: linear-gradient(90deg, var(--green), #34d399); }
.kpi-card.critica::before { background: linear-gradient(90deg, var(--red), #f97316); }
.kpi-card.alta::before { background: linear-gradient(90deg, var(--yellow), #facc15); }

.kpi-label {
  font-size: 10px;
  font-family: var(--mono);
  color: var(--text-muted);
  text-transform: uppercase;
  letter-spacing: 1.2px;
  margin-bottom: 10px;
}
.kpi-value {
  font-size: 36px;
  font-weight: 700;
  line-height: 1;
  color: var(--text);
  font-variant-numeric: tabular-nums;
}
.kpi-card.total .kpi-value { color: var(--cyan); }
.kpi-card.aberto .kpi-value { color: var(--yellow); }
.kpi-card.andamento .kpi-value { color: var(--accent); }
.kpi-card.finalizado .kpi-value { color: var(--green); }
.kpi-card.critica .kpi-value { color: var(--red); }
.kpi-card.alta .kpi-value { color: var(--yellow); }
.kpi-sub {
  font-size: 11px;
  color: var(--text-muted);
  margin-top: 6px;
}

@keyframes fadeUp {
  from { opacity: 0; transform: translateY(12px); }
  to { opacity: 1; transform: translateY(0); }
}

/* ── SECTION GRID ── */
.section-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 20px;
  margin-bottom: 24px;
}
@media (max-width: 760px) { .section-grid { grid-template-columns: 1fr; } }

/* ── PANELS ── */
.panel {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  overflow: hidden;
  animation: fadeUp 0.5s ease both;
}
.panel-header {
  padding: 16px 20px;
  border-bottom: 1px solid var(--border);
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.panel-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--text);
  display: flex;
  align-items: center;
  gap: 8px;
}
.panel-title .dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--accent);
}
.panel-body { padding: 20px; }

/* ── BAR CHART ── */
.bar-chart { display: flex; flex-direction: column; gap: 10px; }
.bar-row { display: flex; flex-direction: column; gap: 4px; }
.bar-meta { display: flex; justify-content: space-between; font-size: 11.5px; }
.bar-name { color: var(--text-dim); font-weight: 500; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 200px; }
.bar-count { font-family: var(--mono); color: var(--text-muted); }
.bar-track {
  height: 6px;
  background: var(--surface3);
  border-radius: 4px;
  overflow: hidden;
}
.bar-fill {
  height: 100%;
  border-radius: 4px;
  background: linear-gradient(90deg, var(--accent2), var(--cyan));
  transition: width 0.8s cubic-bezier(0.16,1,0.3,1);
}

/* ── DONUT CHART ── */
.donut-wrap {
  display: flex;
  align-items: center;
  gap: 24px;
  flex-wrap: wrap;
}
.donut-svg { flex-shrink: 0; }
.donut-legend { display: flex; flex-direction: column; gap: 8px; }
.legend-row { display: flex; align-items: center; gap: 8px; font-size: 12px; color: var(--text-dim); }
.legend-dot { width: 9px; height: 9px; border-radius: 50%; flex-shrink: 0; }
.legend-val { font-family: var(--mono); color: var(--text-muted); font-size: 11px; }

/* ── RECENT TABLE ── */
.panel-full {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  overflow: hidden;
  animation: fadeUp 0.6s ease both;
  margin-bottom: 24px;
}
table { width: 100%; border-collapse: collapse; }
thead th {
  padding: 11px 16px;
  text-align: left;
  font-size: 10px;
  font-family: var(--mono);
  color: var(--text-muted);
  text-transform: uppercase;
  letter-spacing: 1px;
  background: var(--surface2);
  border-bottom: 1px solid var(--border);
  white-space: nowrap;
}
tbody tr {
  border-bottom: 1px solid var(--border);
  transition: background 0.15s;
}
tbody tr:last-child { border-bottom: none; }
tbody tr:hover { background: var(--surface2); }
tbody td {
  padding: 11px 16px;
  font-size: 12.5px;
  color: var(--text-dim);
  vertical-align: middle;
}
.td-id { font-family: var(--mono); color: var(--accent); font-weight: 500; }
.td-desc {
  max-width: 280px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  color: var(--text);
}
.td-date { font-family: var(--mono); font-size: 11px; white-space: nowrap; }

/* ── BADGES ── */
.badge {
  display: inline-flex;
  align-items: center;
  padding: 2px 8px;
  border-radius: 5px;
  font-size: 10.5px;
  font-family: var(--mono);
  font-weight: 500;
  white-space: nowrap;
}
.badge-critica { background: rgba(239,68,68,0.12); color: #f87171; border: 1px solid rgba(239,68,68,0.25); }
.badge-alta    { background: rgba(245,158,11,0.12); color: #fbbf24; border: 1px solid rgba(245,158,11,0.25); }
.badge-media   { background: rgba(59,130,246,0.12); color: #60a5fa; border: 1px solid rgba(59,130,246,0.25); }
.badge-baixa   { background: rgba(16,185,129,0.12); color: #34d399; border: 1px solid rgba(16,185,129,0.25); }
.badge-aberto      { background: rgba(245,158,11,0.10); color: #fbbf24; border: 1px solid rgba(245,158,11,0.2); }
.badge-andamento   { background: rgba(59,130,246,0.10); color: #60a5fa; border: 1px solid rgba(59,130,246,0.2); }
.badge-finalizado  { background: rgba(16,185,129,0.10); color: #34d399; border: 1px solid rgba(16,185,129,0.2); }

/* ── LOADING / EMPTY ── */
.state-loading, .state-empty {
  padding: 48px 20px;
  text-align: center;
  color: var(--text-muted);
  font-size: 13px;
}
.spinner {
  width: 28px;
  height: 28px;
  border: 2px solid var(--border);
  border-top-color: var(--accent);
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
  margin: 0 auto 14px;
}
@keyframes spin { to { transform: rotate(360deg); } }
</style>
</head>
<body>

<div class="topbar">
  <div class="topbar-brand">
    <div class="topbar-logo">KD</div>
    <div>
      <div class="topbar-title">K-Desk</div>
      <div class="topbar-sub">Relatório analítico</div>
    </div>
  </div>
  <div class="topbar-actions">
    <a class="btn-outline" href="/chat">← Voltar ao chat</a>
    <a class="btn-accent" href="/api/tickets/export.csv" id="btn-csv">
      <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
      Exportar Excel (CSV)
    </a>
  </div>
</div>

<div class="container">

  <div class="page-header">
    <div>
      <h1>Painel de Atendimentos</h1>
      <p>Visão consolidada de todos os chamados registrados pelo agente K-Desk</p>
    </div>
    <div class="refresh-info">
      <div class="live-dot"></div>
      <span id="last-update">carregando...</span>
    </div>
  </div>

  <!-- KPIs -->
  <div class="kpi-grid" id="kpi-grid">
    <div class="kpi-card total">
      <div class="kpi-label">Total de chamados</div>
      <div class="kpi-value" id="kpi-total">—</div>
      <div class="kpi-sub">todos os registros</div>
    </div>
    <div class="kpi-card aberto">
      <div class="kpi-label">Abertos</div>
      <div class="kpi-value" id="kpi-aberto">—</div>
      <div class="kpi-sub">aguardando atendimento</div>
    </div>
    <div class="kpi-card andamento">
      <div class="kpi-label">Em andamento</div>
      <div class="kpi-value" id="kpi-andamento">—</div>
      <div class="kpi-sub">sendo atendidos</div>
    </div>
    <div class="kpi-card finalizado">
      <div class="kpi-label">Finalizados</div>
      <div class="kpi-value" id="kpi-finalizado">—</div>
      <div class="kpi-sub">concluídos</div>
    </div>
    <div class="kpi-card critica">
      <div class="kpi-label">Prioridade crítica</div>
      <div class="kpi-value" id="kpi-critica">—</div>
      <div class="kpi-sub">requerem atenção imediata</div>
    </div>
    <div class="kpi-card alta">
      <div class="kpi-label">Prioridade alta</div>
      <div class="kpi-value" id="kpi-alta">—</div>
      <div class="kpi-sub">atendimento urgente</div>
    </div>
  </div>

  <!-- Charts Row -->
  <div class="section-grid">
    <div class="panel">
      <div class="panel-header">
        <div class="panel-title"><div class="dot"></div>Chamados por serviço</div>
      </div>
      <div class="panel-body">
        <div class="bar-chart" id="chart-service">
          <div class="state-loading"><div class="spinner"></div>Carregando...</div>
        </div>
      </div>
    </div>

    <div class="panel">
      <div class="panel-header">
        <div class="panel-title"><div class="dot" style="background:var(--purple)"></div>Distribuição por prioridade</div>
      </div>
      <div class="panel-body">
        <div id="chart-priority">
          <div class="state-loading"><div class="spinner"></div>Carregando...</div>
        </div>
      </div>
    </div>
  </div>

  <!-- Category bar chart -->
  <div class="panel-full">
    <div class="panel-header">
      <div class="panel-title"><div class="dot" style="background:var(--cyan)"></div>Chamados por categoria</div>
    </div>
    <div class="panel-body">
      <div class="bar-chart" id="chart-category">
        <div class="state-loading"><div class="spinner"></div>Carregando...</div>
      </div>
    </div>
  </div>

  <!-- Recent tickets -->
  <div class="panel-full">
    <div class="panel-header">
      <div class="panel-title"><div class="dot" style="background:var(--green)"></div>Chamados recentes</div>
      <span style="font-size:11px;font-family:var(--mono);color:var(--text-muted)" id="table-count"></span>
    </div>
    <div id="table-wrap">
      <div class="state-loading"><div class="spinner"></div>Carregando dados...</div>
    </div>
  </div>

</div>

<script>
const COLORS_PRI = {
  'Crítica': '#ef4444', 'Alta': '#f59e0b', 'Média': '#3b82f6', 'Baixa': '#10b981'
};
const BADGE_PRI = {
  'Crítica':'badge-critica','Alta':'badge-alta','Média':'badge-media','Baixa':'badge-baixa',
};
const BADGE_STATUS = {
  'Aberto':'badge-aberto','Em andamento':'badge-andamento','Finalizado':'badge-finalizado'
};

function norm(s){ return (s||'').normalize('NFD').replace(/[\u0300-\u036f]/g,'').toLowerCase(); }

function count(arr, key){
  return arr.reduce((acc, t) => {
    const v = t[key] || 'Desconhecido';
    acc[v] = (acc[v]||0)+1;
    return acc;
  }, {});
}

function renderBarChart(containerId, data, maxVal) {
  const container = document.getElementById(containerId);
  if(!data || Object.keys(data).length === 0){
    container.innerHTML = '<div class="state-empty">Sem dados disponíveis.</div>';
    return;
  }
  const sorted = Object.entries(data).sort((a,b) => b[1]-a[1]).slice(0,8);
  const max = maxVal || sorted[0][1] || 1;
  container.innerHTML = sorted.map(([name, val]) => {
    const pct = Math.round((val/max)*100);
    return `<div class="bar-row">
      <div class="bar-meta">
        <span class="bar-name" title="${name}">${name}</span>
        <span class="bar-count">${val}</span>
      </div>
      <div class="bar-track"><div class="bar-fill" style="width:0%" data-w="${pct}%"></div></div>
    </div>`;
  }).join('');
  // Animate
  requestAnimationFrame(()=>{
    container.querySelectorAll('.bar-fill').forEach(el=>{
      setTimeout(()=>{ el.style.width = el.dataset.w; }, 80);
    });
  });
}

function renderDonut(containerId, data) {
  const container = document.getElementById(containerId);
  if(!data || Object.keys(data).length === 0){
    container.innerHTML = '<div class="state-empty">Sem dados disponíveis.</div>';
    return;
  }
  const entries = Object.entries(data);
  const total = entries.reduce((s,[,v])=>s+v,0);
  const R = 52, cx = 64, cy = 64, r = R, ri = 34;
  let angle = -Math.PI/2;
  const slices = entries.map(([name, val]) => {
    const color = COLORS_PRI[name] || '#475569';
    const frac = val/total;
    const a1 = angle;
    const a2 = angle + frac * 2 * Math.PI;
    angle = a2;
    const x1=cx+R*Math.cos(a1), y1=cy+R*Math.sin(a1);
    const x2=cx+R*Math.cos(a2), y2=cy+R*Math.sin(a2);
    const xi1=cx+ri*Math.cos(a1), yi1=cy+ri*Math.sin(a1);
    const xi2=cx+ri*Math.cos(a2), yi2=cy+ri*Math.sin(a2);
    const large = frac > 0.5 ? 1 : 0;
    const d = `M${x1},${y1} A${R},${R},0,${large},1,${x2},${y2} L${xi2},${yi2} A${ri},${ri},0,${large},0,${xi1},${yi1} Z`;
    return { name, val, color, d, pct: Math.round(frac*100) };
  });

  const svgSlices = slices.map(s =>
    `<path d="${s.d}" fill="${s.color}" opacity="0.85" stroke="var(--surface)" stroke-width="1.5"/>`
  ).join('');

  const legend = slices.map(s =>
    `<div class="legend-row">
      <div class="legend-dot" style="background:${s.color}"></div>
      <span>${s.name}</span>
      <span class="legend-val">${s.val} (${s.pct}%)</span>
    </div>`
  ).join('');

  container.innerHTML = `<div class="donut-wrap">
    <svg class="donut-svg" width="128" height="128" viewBox="0 0 128 128">
      ${svgSlices}
      <circle cx="${cx}" cy="${cy}" r="${ri}" fill="var(--surface)"/>
      <text x="${cx}" y="${cy}" text-anchor="middle" dy="5" font-size="13" font-weight="700" font-family="var(--mono)" fill="var(--text)">${total}</text>
    </svg>
    <div class="donut-legend">${legend}</div>
  </div>`;
}

function renderTable(tickets) {
  const wrap = document.getElementById('table-wrap');
  const count = document.getElementById('table-count');
  if(!tickets || tickets.length === 0){
    wrap.innerHTML = '<div class="state-empty">Nenhum chamado registrado ainda.</div>';
    count.textContent = '';
    return;
  }
  count.textContent = tickets.length + ' chamado' + (tickets.length!==1?'s':'');
  const rows = tickets.slice(0, 50).map(t => {
    const pri = t.priority || 'Média';
    const bPri = BADGE_PRI[pri] || 'badge-media';
    const st = t.status || 'Aberto';
    const bSt = BADGE_STATUS[st] || 'badge-aberto';
    const dt = (t.created_at||'').replace('T',' ').slice(0,16);
    const desc = (t.description||'').slice(0,80) + ((t.description||'').length>80?'…':'');
    return `<tr>
      <td class="td-id">${t.ticket_id||'—'}</td>
      <td><span class="td-desc" title="${(t.description||'').replace(/"/g,'')}">${desc}</span></td>
      <td>${t.service||'—'}</td>
      <td><span class="badge ${bPri}">${pri}</span></td>
      <td><span class="badge ${bSt}">${st}</span></td>
      <td class="td-date">${dt||'—'}</td>
    </tr>`;
  }).join('');

  wrap.innerHTML = `<table>
    <thead><tr>
      <th>Ticket</th><th>Descrição</th><th>Serviço</th><th>Prioridade</th><th>Status</th><th>Data</th>
    </tr></thead>
    <tbody>${rows}</tbody>
  </table>`;
}

async function loadData(){
  try {
    const res = await fetch('/api/tickets?limit=300');
    if(!res.ok) throw new Error('HTTP ' + res.status);
    const data = await res.json();
    if(data.error) throw new Error(data.error);
    const tickets = data.tickets || [];

    // KPIs
    const byStatus = count(tickets, 'status');
    const byPri = count(tickets, 'priority');
    document.getElementById('kpi-total').textContent = tickets.length;
    document.getElementById('kpi-aberto').textContent = byStatus['Aberto']||0;
    document.getElementById('kpi-andamento').textContent = byStatus['Em andamento']||0;
    document.getElementById('kpi-finalizado').textContent = byStatus['Finalizado']||0;
    document.getElementById('kpi-critica').textContent = byPri['Crítica']||0;
    document.getElementById('kpi-alta').textContent = byPri['Alta']||0;

    // Charts
    const byService = count(tickets, 'service');
    const byCategory = count(tickets, 'category');
    renderBarChart('chart-service', byService);
    renderBarChart('chart-category', byCategory);
    renderDonut('chart-priority', byPri);
    renderTable(tickets);

    // Last update
    const now = new Date().toLocaleTimeString('pt-BR',{hour:'2-digit',minute:'2-digit'});
    document.getElementById('last-update').textContent = 'Atualizado às ' + now;

  } catch(err) {
    document.getElementById('kpi-total').textContent = '—';
    document.getElementById('table-wrap').innerHTML =
      '<div class="state-empty">Erro ao carregar dados: ' + err.message + '</div>';
    document.getElementById('last-update').textContent = 'Falha na conexão';
  }
}

loadData();
// Auto-refresh a cada 5s
setInterval(loadData, 5000);
</script>
</body>
</html>"""
    return html, 200


@app.route("/chat", methods=["GET"])
def chat_interface():
    try:
        html_path = Path(__file__).parent / "chat_interface.html"
        html = html_path.read_text(encoding="utf-8")
        return html, 200
    except Exception as e:
        return f"Erro ao carregar interface: {e}", 500
    return html, 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
