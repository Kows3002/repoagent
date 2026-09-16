from pathlib import Path


def detect_project_type(repo_path: str):
    path = Path(repo_path)

    # Search entire repository
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

    # 1. Find the exact file path mentioned in the task
    for file in path.rglob("*"):
        if file.is_file():
            relative = file.relative_to(path).as_posix().lower()

            if relative in task_lower:
                files.append(file)

    # 2. CI/CD workflow support
    if not files and "ci" in task_lower:
        workflow = path / ".github" / "workflows"

        if workflow.exists():
            files.extend(workflow.glob("*.yml"))

    # 3. Fallback important files
    if not files:
        for name in ["package.json", "requirements.txt", "pyproject.toml", "README.md"]:
            files.extend(path.rglob(name))

    return [str(f) for f in files]


def read_files(file_paths):
    data = {}

    for file in file_paths:
        try:
            content = Path(file).read_text(
                encoding="utf-8",
                errors="ignore"
            )
            data[Path(file).name] = content
        except Exception:
            pass

    return data