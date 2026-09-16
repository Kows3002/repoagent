from ollama import chat

def analyze_code(task: str, project_type: str, files: dict):
    prompt = f"""
You are an expert software engineer.

Task:
{task}

Project Type:
{project_type}

Analyze the repository and explain:
1. What this project is.
2. Important files.
3. Suggested next steps.
"""

    # Reduce context size for faster inference
    for name, content in files.items():
        prompt += f"\n### {name}\n{content[:1000]}\n"

    response = chat(
        model="llama3.2:latest",
        messages=[
            {
                "role": "user",
                "content": prompt
            }
        ]
    )

    return response.message.content