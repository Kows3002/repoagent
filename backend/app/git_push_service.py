origin_url = repo.remotes.origin.url

# Convert https://github.com/... into authenticated URL
auth_url = origin_url.replace(
    "https://",
    f"https://{username}:{token}@"
)

repo.git.remote("set-url", "origin", auth_url)