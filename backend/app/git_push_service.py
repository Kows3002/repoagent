import os

username = os.getenv("GITHUB_USERNAME")
token = os.getenv("GITHUB_TOKEN")

repo.git.remote("origin").set_url(
    f"https://{username}:{token}@github.com/Kows3002/visitor-pass-management-system.git"
)