import argparse
import csv
from pathlib import Path


REQUIRED_COLUMNS = [
    "article_id",
    "article_title",
    "service",
    "category",
    "knowledge_type",
    "user_need",
    "symptoms",
    "user_description_examples",
    "diagnostic_questions",
    "required_information",
    "probable_cause",
    "resolution_steps",
    "workaround",
    "escalation_criteria",
    "priority_guidance",
    "estimated_resolution_time",
    "agent_response_guidance",
    "keywords",
    "owner_role",
    "review_frequency",
    "status",
    "version",
]


def read_rows(path: Path):
    last_error = None
    for enc in ("utf-8-sig", "latin-1"):
        try:
            with path.open("r", encoding=enc, newline="") as f:
                reader = csv.DictReader(f)
                rows = list(reader)
                return reader.fieldnames or [], rows, enc
        except UnicodeDecodeError as exc:
            last_error = exc
    raise RuntimeError(f"Falha ao ler CSV: {last_error}")


def validate_columns(found: list[str]):
    missing = [c for c in REQUIRED_COLUMNS if c not in found]
    if missing:
        raise RuntimeError(f"CSV inválido. Colunas ausentes: {missing}")


def write_normalized(dest: Path, rows: list[dict]):
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=REQUIRED_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in REQUIRED_COLUMNS})


def main():
    parser = argparse.ArgumentParser(description="Importa e valida base de conhecimento CSV do K-desk.")
    parser.add_argument("--source", required=True, help="CSV de origem")
    parser.add_argument("--target", default="data/support_knowledge_base.csv", help="CSV alvo no projeto")
    args = parser.parse_args()

    src = Path(args.source).expanduser()
    if not src.exists():
        raise FileNotFoundError(f"Arquivo não encontrado: {src}")

    dst = Path(args.target).expanduser()

    columns, rows, used_encoding = read_rows(src)
    validate_columns(columns)
    if not rows:
        raise RuntimeError("CSV sem linhas de dados.")

    write_normalized(dst, rows)

    print(f"Importação concluída.")
    print(f"- Origem: {src}")
    print(f"- Destino: {dst}")
    print(f"- Linhas: {len(rows)}")
    print(f"- Encoding lido: {used_encoding}")


if __name__ == "__main__":
    main()
