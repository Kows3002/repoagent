import os
import json
import re
from dotenv import load_dotenv
from groq import Groq

load_dotenv()

client = Groq(api_key=os.getenv("GROQ_API_KEY"))


def generate_patch(task: str, filename: str, content: str):
    prompt = f"""
Return ONLY one complete JSON object.

Do not use markdown.
Do not explain anything.
Do not truncate the response.

JSON format:
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
        temperature=0,
        max_completion_tokens=8192
    )

    text = response.choices[0].message.content or ""
    text = text.strip()

    print("\n===== RAW GROQ RESPONSE =====")
    print(text)

    # Remove markdown code fences
    text = re.sub(r"^```json\s*", "", text)
    text = re.sub(r"^```\s*", "", text)
    text = re.sub(r"\s*```$", "", text)

    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise Exception(f"Invalid JSON returned:\n{text}") from e