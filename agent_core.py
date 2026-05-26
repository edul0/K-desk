import csv
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Tuple


@dataclass
class KBArticle:
    article_id: str
    title: str
    service: str
    category: str
    diagnostic_questions: List[str]
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
    return [p.strip() for p in re.split(r"[,;]", raw) if p.strip()]


def split_questions(raw: str) -> List[str]:
    """Divide perguntas separadas por ? preservando o ?"""
    if not raw:
        return []
    parts = re.split(r'\?\s*', raw)
    questions = [p.strip() + "?" for p in parts if p.strip()]
    return questions[:3]  # máximo 3 perguntas


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
                diagnostic_questions=split_questions(row.get("diagnostic_questions", "")),
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
                " ".join([art.title, art.service, art.category, " ".join(art.keywords), art.examples])
            )
            articles.append(art)
    return articles


# Palavras que ampliam o vocabulário de busca sem estar nos keywords exatos
_SYNONYMS: Dict[str, List[str]] = {
    "senha": ["password", "pass", "esqueci", "expirou", "redefinir", "reset", "trocar", "alterar", "desbloquear"],
    "acesso": ["entrar", "login", "acessar", "conectar"],
    "bloqueado": ["bloqueou", "travou", "bloqueada", "locked"],
    "email": ["e-mail", "correio", "mensagem", "caixa"],
    "impressora": ["imprimir", "impressao", "print"],
    "wifi": ["wi-fi", "rede sem fio", "wireless", "internet", "net", "conexao", "conexão", "cabo", "sinal", "sem rede", "sem internet"],
    "vpn": ["acesso remoto", "rede privada"],
    "lento": ["devagar", "travando", "demora", "lentidao"],
    "virus": ["malware", "infectado", "antivirus"],
    "phishing": ["email suspeito", "link suspeito", "fraude"],
}


def _expand(text: str) -> str:
    """Adiciona sinônimos ao texto normalizado para melhorar o match."""
    norm = normalize(text)
    extras = []
    for key, syns in _SYNONYMS.items():
        if key in norm or any(normalize(s) in norm for s in syns):
            extras.append(key)
            extras.extend(syns)
    return norm + " " + " ".join(extras)


def score_article(user_text: str, article: KBArticle) -> float:
    user_exp = _expand(user_text)
    user_tokens = set(user_exp.split())
    if not user_tokens:
        return 0.0
    keyword_hits = sum(1 for k in article.keywords if normalize(k) in user_exp)
    overlap = len(user_tokens.intersection(set(article.normalized_blob.split())))
    # Bônus se o título do artigo aparece no texto expandido
    title_bonus = 1.5 if any(t in user_exp for t in normalize(article.title).split() if len(t) > 4) else 0.0
    return keyword_hits * 2.5 + overlap * 0.4 + title_bonus


def classify_priority(text: str, kb_priority: str) -> str:
    t = normalize(text)
    if any(k in t for k in ["phishing", "malware", "virus", "compromet", "mfa", "invas"]):
        return "Crítica"
    if any(k in t for k in ["parou", "fora", "nao liga", "sem acesso", "bloquead", "indisponivel"]):
        return "Alta"
    kbp = normalize(kb_priority)
    if "critica" in kbp:
        return "Crítica"
    if "alta" in kbp:
        return "Alta"
    if "media" in kbp:
        return "Média"
    if "baixa" in kbp:
        return "Baixa"
    return "Média"


def estimate_eta(priority: str, kb_eta: str) -> str:
    if priority == "Crítica":
        return "Resposta imediata e contenção em até 1 hora útil"
    if priority == "Alta":
        return kb_eta or "Até 4 horas úteis"
    if priority == "Média":
        return kb_eta or "Até 1 dia útil"
    return kb_eta or "Até 2 dias úteis"


def is_security_case(text: str, article: KBArticle) -> bool:
    combined = normalize(text + " " + article.category + " " + article.service)
    return any(k in combined for k in ["seguranca", "phishing", "malware", "compromet", "invasao"])


def triage(description: str, answers: Dict[str, str], articles: List[KBArticle]) -> Tuple[str, Dict]:
    """
    answers: dict com chaves sendo índices string ("0", "1", "2") das perguntas
    respondidas pelo usuário, mapeados aos textos das respostas.
    """
    # Detecta cumprimentos genéricos ou entradas curtas demais/sem contexto de TI
    norm_desc = normalize(description)
    greetings = {"oi", "ola", "bom dia", "boa tarde", "boa noite", "tudo bem", "ola tudo bem", "hello", "hi", "hey"}
    
    ranked = sorted(
        ((a, score_article(description, a)) for a in articles),
        key=lambda x: x[1],
        reverse=True
    )
    best, best_score = ranked[0]
    second_score = ranked[1][1] if len(ranked) > 1 else 0.0

    if norm_desc in greetings or len(norm_desc) < 3 or best_score < 0.2:
        return "need_more_info", {
            "kb_article_id": "",
            "kb_article_title": "",
            "questions": [],
            "next_question_index": 0,
            "message": "Olá! Como posso te ajudar com o suporte de TI hoje? Por favor, descreva o problema ou incidente que você está enfrentando com o máximo de detalhes possível.",
            "is_greeting": True
        }

    # Threshold reduzido: score >= 1.5 e diferença >= 0.8 já é match claro
    ambiguous = best_score < 1.5 or (best_score - second_score) < 0.8

    questions = best.diagnostic_questions  # lista de strings

    # Número de respostas já coletadas (chaves "0", "1", "2"…)
    n_answered = sum(1 for k in answers if str(k).isdigit())
    # Máximo de perguntas: fazemos no máximo 2 rounds (já que temos até 3 perguntas)
    max_questions = min(len(questions), 2)

    # Se ambíguo e ainda há perguntas não feitas
    if ambiguous and n_answered < max_questions and questions:
        next_idx = n_answered  # índice da próxima pergunta
        next_q = questions[next_idx] if next_idx < len(questions) else questions[-1]
        msg = (
            "Entendi. Para registrar corretamente, preciso de mais uma informação: "
            f"{next_q}"
        )
        # Lista das perguntas ainda não respondidas (por índice)
        remaining = [
            questions[i]
            for i in range(next_idx, len(questions))
        ]
        return "need_more_info", {
            "kb_article_id": best.article_id,
            "kb_article_title": best.title,
            "questions": remaining,
            "next_question_index": next_idx,
            "message": msg,
        }

    # Informações suficientes — registra chamado
    all_context = description + " " + " ".join(str(v) for v in answers.values())
    priority = classify_priority(all_context, best.priority_guidance)
    eta = estimate_eta(priority, best.estimated_resolution_time)
    escalation = is_security_case(description, best) or priority == "Crítica"

    return "ready", {
        "article": best,
        "priority": priority,
        "eta": eta,
        "escalation": escalation,
    }
