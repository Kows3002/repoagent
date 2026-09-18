from pathlib import Path


def detect_project_type(repo_path: str):
    path = Path(repo_path)

    if list(path.rglob("package.json")):
        return "React/Node"

    if list(path.rglob("requirements.txt")):
        return "Python"

    if list(path.rglob("pyproject.toml")):
        return "Python"

    return "Unknown"


def find_relevant_files(repo_path: str, task: str):
    path = Path(repo_path)
    files = []
    task_lower = task.lower()

    # Find exact file mentioned in task
    for file in path.rglob("*"):
        if file.is_file():
            relative = file.relative_to(path).as_posix().lower()
            if relative in task_lower:
                files.append(file)

    # CI workflow support
    if not files and "ci" in task_lower:
        workflow = path / ".github" / "workflows"
        if workflow.exists():
            files.extend(workflow.glob("*.yml"))

    # Fallback important files
    if not files:
        for name in [
            "package.json",
            "requirements.txt",
            "pyproject.toml",
            "README.md",
        ]:
            files.extend(path.rglob(name))

    # Return only files that actually exist
    return [str(f) for f in files if f.exists()]


def read_files(file_paths):
    data = {}

    for file in file_paths:
        path = Path(file)

        if not path.exists():
            print(f"Skipping missing file: {path}")
            continue

        try:
            data[path.name] = path.read_text(
                encoding="utf-8",
                errors="ignore",
            )
        except Exception as e:
            print(f"Error reading {path}: {e}")

    return data