from pathlib import Path

def apply_patch(repo_folder: str, patch: dict):

    file_path = Path(repo_folder) / patch["filename"]

    file_path.write_text(
        patch["updated_code"],
        encoding="utf-8"
    )

    return str(file_path)