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
    diagnostic_questions: str
    required_information: List[str]
    resolution_steps: str
    workaround: str
    escalation_criteria: str
    priority_guidance: str
    estimated_resolution_time: str
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
                keywords=split_csv_list(row.get("keywords", "")),
                examples=row.get("user_description_examples", ""),
            )
            art.normalized_blob = normalize(
                " ".join([art.title, art.service, art.category, " ".join(art.keywords), art.examples])
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
    ranked = sorted(((a, score_article(description, a)) for a in articles), key=lambda x: x[1], reverse=True)
    best, best_score = ranked[0]
    second_score = ranked[1][1] if len(ranked) > 1 else 0.0
    ambiguous = best_score < 2.0 or (best_score - second_score) < 1.2

    if ambiguous and not answers:
        return "need_more_info", {
            "kb_article_id": best.article_id,
            "kb_article_title": best.title,
            "questions": split_csv_list(best.diagnostic_questions)[:4],
            "message": "Preciso de mais detalhes para classificar corretamente.",
        }

    missing_required = []
    combined = normalize(description + " " + " ".join(str(v) for v in answers.values()))
    for req in best.required_information[:5]:
        probe = normalize(req).split()
        if probe and not any(token in combined for token in probe):
            missing_required.append(req)

    if missing_required:
        return "missing_required", {
            "required_fields": missing_required,
            "message": "Antes do registro, preciso das informações obrigatórias.",
            "kb_article_id": best.article_id,
            "kb_article_title": best.title,
        }

    priority = classify_priority(description + " " + " ".join(str(v) for v in answers.values()), best.priority_guidance)
    eta = estimate_eta(priority, best.estimated_resolution_time)
    escalation = is_security_case(description, best) or priority == "Crítica"

    return "ready", {
        "article": best,
        "priority": priority,
        "eta": eta,
        "escalation": escalation,
    }
