import os
import json
import re
from groq import Groq

client = Groq(api_key=os.getenv("GROQ_API_KEY"))


def generate_patch(task: str, filename: str, content: str):
    prompt = f"""
Return ONLY a valid JSON object.

{{
  "filename": "{filename}",
  "updated_code": "FULL updated file content"
}}

Modify ONLY this file.

Task:
{task}

Current file:
{content}
"""

    response = client.chat.completions.create(
        model="openai/gpt-oss-20b",
        messages=[
            {"role": "user", "content": prompt}
        ],
        temperature=0
    )

    text = response.choices[0].message.content.strip()

    # Debug: print raw AI response
    print("\n===== RAW GROQ RESPONSE =====")
    print(text)

    # Remove ```json ... ``` wrappers if present
    text = re.sub(r"^```json\s*", "", text)
    text = re.sub(r"^```\s*", "", text)
    text = re.sub(r"\s*```$", "", text)

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        raise Exception(f"Invalid JSON returned:\n{text}")