from pathlib import Path
import os


ROOT_DIR = Path(__file__).resolve().parent
OUTPUT_FILE = ROOT_DIR / "projeto_completo.txt"

IGNORED_DIRECTORIES = {
    ".git",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "build",
    "dist",
}

SENSITIVE_FILE_NAMES = {
    ".env",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
}

SENSITIVE_SUFFIXES = {
    ".key",
    ".pem",
    ".p12",
    ".pfx",
    ".keystore",
}


def is_sensitive_file(path: Path) -> bool:
    name = path.name.lower()
    return (
        name in SENSITIVE_FILE_NAMES
        or name.startswith(".env.")
        or path.suffix.lower() in SENSITIVE_SUFFIXES
    )


def collect_project_files():
    files = []

    def raise_walk_error(error):
        raise error

    for current_dir, directories, file_names in os.walk(
        ROOT_DIR, topdown=True, onerror=raise_walk_error, followlinks=False
    ):
        current_path = Path(current_dir)
        directories[:] = sorted(
            name
            for name in directories
            if name not in IGNORED_DIRECTORIES
            and not (current_path / name).is_symlink()
        )

        for name in sorted(file_names):
            path = current_path / name
            if path == OUTPUT_FILE or path.is_symlink():
                continue
            files.append(path)

    return sorted(files, key=lambda path: path.relative_to(ROOT_DIR).as_posix().lower())


def render_tree(files):
    lines = [ROOT_DIR.name + "/"]
    for path in files:
        lines.append(f"  {path.relative_to(ROOT_DIR).as_posix()}")
    return lines


def write_export():
    files = collect_project_files()
    with OUTPUT_FILE.open("w", encoding="utf-8", newline="\n") as output:
        output.write("EXPORTAÇÃO DO PROJETO\n")
        output.write("Arquivos sensíveis, binários e diretórios de dependências/cache são omitidos.\n\n")
        output.write("ESTRUTURA DE ARQUIVOS\n")
        output.write("\n".join(render_tree(files)))
        output.write("\n\nCONTEÚDO DOS ARQUIVOS\n")

        for path in files:
            relative_path = path.relative_to(ROOT_DIR).as_posix()
            output.write(f"\n{'=' * 80}\nARQUIVO: {relative_path}\n{'=' * 80}\n")

            if is_sensitive_file(path):
                output.write("[Conteúdo omitido: arquivo sensível]\n")
                continue

            try:
                content = path.read_bytes()
                if b"\x00" in content:
                    output.write("[Conteúdo omitido: arquivo binário]\n")
                    continue
                text = content.decode("utf-8")
            except UnicodeDecodeError:
                output.write("[Conteúdo omitido: arquivo binário ou codificação não UTF-8]\n")
                continue

            output.write(text)
            if text and not text.endswith("\n"):
                output.write("\n")

    print(f"Exportação concluída: {OUTPUT_FILE}")


if __name__ == "__main__":
    write_export()
