import os
from groq import Groq

client = Groq(api_key=os.getenv("GROQ_API_KEY"))

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

    for name, content in files.items():
        prompt += f"\n### {name}\n{content[:1000]}\n"

    response = client.chat.completions.create(
        model="openai/gpt-oss-20b",
        messages=[
            {"role": "user", "content": prompt}
        ],
        temperature=0
    )

    return response.choices[0].message.content