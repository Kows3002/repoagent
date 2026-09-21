import os
import json
import re
from dotenv import load_dotenv
from groq import Groq

load_dotenv()

client = Groq(api_key=os.getenv("GROQ_API_KEY"))


def generate_patch(task: str, filename: str, content: str):
    prompt = f"""
You are an expert software engineer.

Modify ONLY the given file.

Return ONLY one valid JSON object in this exact format:

{{
  "filename": "{filename}",
  "updated_code": "<FULL updated file content>"
}}

Rules:
- Output JSON only.
- No markdown.
- No explanations.
- Preserve every line except the requested change.

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
        max_completion_tokens=16384
    )

    text = (response.choices[0].message.content or "").strip()

    print("\n===== RAW GROQ RESPONSE =====")
    print(text)

    # Remove markdown fences if present
    text = re.sub(r"^```json\s*", "", text)
    text = re.sub(r"^```\s*", "", text)
    text = re.sub(r"\s*```$", "", text)

    # Extract JSON object safely
    start = text.find("{")
    end = text.rfind("}")

    if start == -1 or end == -1:
        raise Exception(f"Invalid JSON returned:\n{text}")

    json_text = text[start:end + 1]

    try:
        return json.loads(json_text)
    except json.JSONDecodeError as e:
        raise Exception(f"Invalid JSON returned:\n{json_text}") from e