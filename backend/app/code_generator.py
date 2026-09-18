import os
import json
import re
from groq import Groq

client = Groq(api_key=os.getenv("GROQ_API_KEY"))


def generate_patch(task: str, filename: str, content: str):
    prompt = f"""
Modify ONLY this file.

Return ONLY valid JSON.

{{
  "filename": "{filename}",
  "updated_code": "FULL updated file content"
}}

Task:
{task}

Current file:
{content}
"""

    response = client.chat.completions.create(
        model="openai/gpt-oss-20b",
        messages=[
            {
                "role": "user",
                "content": prompt
            }
        ],
        temperature=0
    )

    text = response.choices[0].message.content.strip()

    # Extract JSON safely
    match = re.search(r"\{[\s\S]*\}", text)

    if not match:
        raise Exception(f"Invalid JSON returned:\n{text}")

    return json.loads(match.group())