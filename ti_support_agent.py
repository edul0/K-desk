import csv
import json
import os
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple

BASE_DIR = Path(r"C:\Users\dudul\Documents\Codex\2026-05-18\files-mentioned-by-the-user-support")
KB_PATH = Path(r"C:\Users\dudul\Downloads\support_knowledge_base (1).csv")
TICKETS_PATH = BASE_DIR / "service_requests.csv"


@dataclass
class KBArticle:
    article_id: str
    title: str
    service: str
    category: str
    diagnostic_questions: str
    required_information: List[str]
    resolution_steps: str
    workaround: str
    escalation_criteria: str
    priority_guidance: str
    estimated_resolution_time: str
    agent_response_guidance: str
    keywords: List[str]
    examples: str
    normalized_blob: str = field(default="")


def normalize(text: str) -> str:
    text = text or ""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower()
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def split_csv_list(raw: str) -> List[str]:
    if not raw:
        return []
    parts = [p.strip() for p in re.split(r"[,;]", raw) if p.strip()]
    return parts


def load_kb(path: Path) -> List[KBArticle]:
    articles: List[KBArticle] = []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            art = KBArticle(
                article_id=row.get("article_id", ""),
                title=row.get("article_title", ""),
                service=row.get("service", ""),
                category=row.get("category", ""),
                diagnostic_questions=row.get("diagnostic_questions", ""),
                required_information=split_csv_list(row.get("required_information", "")),
                resolution_steps=row.get("resolution_steps", ""),
                workaround=row.get("workaround", ""),
                escalation_criteria=row.get("escalation_criteria", ""),
                priority_guidance=row.get("priority_guidance", ""),
                estimated_resolution_time=row.get("estimated_resolution_time", ""),
                agent_response_guidance=row.get("agent_response_guidance", ""),
                keywords=split_csv_list(row.get("keywords", "")),
                examples=row.get("user_description_examples", ""),
            )
            art.normalized_blob = normalize(
                " ".join(
                    [
                        art.title,
                        art.service,
                        art.category,
                        " ".join(art.keywords),
                        art.examples,
                    ]
                )
            )
            articles.append(art)
    return articles


def score_article(user_text: str, article: KBArticle) -> float:
    user_norm = normalize(user_text)
    user_tokens = set(user_norm.split())
    if not user_tokens:
        return 0.0

    keyword_hits = sum(1 for k in article.keywords if normalize(k) in user_norm)
    overlap = len(user_tokens.intersection(set(article.normalized_blob.split())))

    return keyword_hits * 2.5 + overlap * 0.4


def classify_priority(text: str, kb_priority: str) -> str:
    t = normalize(text)
    if any(k in t for k in ["phishing", "malware", "virus", "compromet", "mfa", "invas"]):
        return "Crítica"
    if any(k in t for k in ["parou", "fora", "nao liga", "sem acesso", "bloquead", "indisponivel"]):
        return "Alta"
    if "media" in normalize(kb_priority):
        return "Média"
    if "baixa" in normalize(kb_priority):
        return "Baixa"
    if "alta" in normalize(kb_priority):
        return "Alta"
    if "critica" in normalize(kb_priority):
        return "Crítica"
    return "Média"


def estimate_eta(priority: str, kb_eta: str) -> str:
    if priority == "Crítica":
        return "Resposta imediata e contenção em até 1 hora útil"
    if priority == "Alta":
        return kb_eta or "Até 4 horas úteis"
    if priority == "Média":
        return kb_eta or "Até 1 dia útil"
    return kb_eta or "Até 2 dias úteis"


def ensure_ticket_file(path: Path) -> None:
    if path.exists():
        return
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "ticket_id",
                "created_at",
                "requester_name",
                "requester_email",
                "description",
                "kb_article_id",
                "service",
                "category",
                "priority",
                "estimated_resolution_time",
                "escalation_required",
                "escalation_reason",
                "collected_fields_json",
                "status",
            ]
        )


def next_ticket_id(path: Path) -> str:
    if not path.exists():
        return "TKT-0001"
    with path.open("r", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    return f"TKT-{len(rows) + 1:04d}"


def register_ticket(
    path: Path,
    requester_name: str,
    requester_email: str,
    description: str,
    article: KBArticle,
    priority: str,
    eta: str,
    collected_fields: Dict[str, str],
    escalation_required: bool,
) -> str:
    ensure_ticket_file(path)
    ticket_id = next_ticket_id(path)
    escalation_reason = article.escalation_criteria if escalation_required else ""

    with path.open("a", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            [
                ticket_id,
                datetime.now().isoformat(timespec="seconds"),
                requester_name,
                requester_email,
                description,
                article.article_id,
                article.service,
                article.category,
                priority,
                eta,
                "sim" if escalation_required else "não",
                escalation_reason,
                json.dumps(collected_fields, ensure_ascii=False),
                "Aberto",
            ]
        )
    return ticket_id


def is_security_case(text: str, article: KBArticle) -> bool:
    combined = normalize(text + " " + article.category + " " + article.service)
    return any(k in combined for k in ["seguranca", "phishing", "malware", "compromet", "invasao"])


def run_chatbot() -> None:
    print("=== Agente Inteligente de Suporte de TI ===")
    print("Digite sua solicitação. Use 'sair' para encerrar.\n")

    articles = load_kb(KB_PATH)
    requester_name = input("Seu nome: ").strip() or "Usuário não identificado"
    requester_email = input("Seu e-mail corporativo: ").strip() or "não informado"

    while True:
        description = input("\nUsuário: ").strip()
        if normalize(description) in {"sair", "exit", "quit"}:
            print("Agente: Atendimento encerrado.")
            break

        ranked = sorted(
            ((a, score_article(description, a)) for a in articles), key=lambda x: x[1], reverse=True
        )

        best, best_score = ranked[0]
        second_score = ranked[1][1] if len(ranked) > 1 else 0.0
        ambiguous = best_score < 2.0 or (best_score - second_score) < 1.2

        collected: Dict[str, str] = {}
        if ambiguous:
            print("Agente: Preciso de alguns detalhes para classificar corretamente sua demanda.")
            for question in split_csv_list(best.diagnostic_questions)[:4]:
                answer = input(f"Agente pergunta: {question} ")
                collected[question] = answer.strip()

        missing_required = []
        if best.required_information:
            combined_text = normalize(description + " " + " ".join(collected.values()))
            for req in best.required_information[:5]:
                probe = normalize(req).split()
                if probe and not any(token in combined_text for token in probe):
                    missing_required.append(req)

        if missing_required:
            print("Agente: Antes do registro, preciso confirmar algumas informações obrigatórias:")
            for req in missing_required:
                val = input(f"- {req}: ")
                collected[req] = val.strip()

        priority = classify_priority(description + " " + " ".join(collected.values()), best.priority_guidance)
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
            collected,
            escalation,
        )

        print("\nAgente: Identifiquei o tema da solicitação e já registrei seu atendimento.")
        print(f"- Ticket: {ticket_id}")
        print(f"- Base relacionada: {best.article_id} - {best.title}")
        print(f"- Serviço/Categoria: {best.service} / {best.category}")
        print(f"- Prioridade automática: {priority}")
        print(f"- Prazo estimado: {eta}")
        print(f"- Escalonamento humano: {'Sim' if escalation else 'Não'}")
        if escalation:
            print(f"- Critério de escalonamento: {best.escalation_criteria}")

        print("\nOrientação inicial:")
        print(f"1) {best.resolution_steps}")
        if best.workaround:
            print(f"2) Contorno recomendado: {best.workaround}")
        print("3) Caso necessário, um analista seguirá com seu atendimento via ticket.\n")


if __name__ == "__main__":
    run_chatbot()
