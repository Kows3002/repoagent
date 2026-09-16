from ollama import chat


def generate_patch(task: str, filename: str, content: str):

    prompt = f"""
You are an expert React developer.

Task:
{task}

Edit ONLY this file.

Return ONLY the complete updated source code.

Rules:
- No explanation
- No markdown
- No ``` blocks
- Return only the code

Current file:

{content}
"""

    response = chat(
        model="llama3.2:latest",
        messages=[
            {
                "role": "user",
                "content": prompt
            }
        ],
        options={
            "temperature": 0
        }
    )

    return {
        "filename": filename,
        "updated_code": response.message.content.strip()
    }