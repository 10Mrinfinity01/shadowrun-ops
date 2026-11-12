import os
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()
client = OpenAI(
    base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
    api_key=os.getenv("GEMINI_API_KEY"),
)
candidates = [
    "gemini-flash-latest", "gemini-3.8-flash", "gemini-3.7-flash",
    "gemini-3.6-flash", "gemini-3.5-flash", "gemini-2.5-flash",
]
for model in candidates:
    try:
        r = client.chat.completions.create(
            model=model, messages=[{"role": "user", "content": "Reply with: OK"}], max_tokens=20)
        print(f"✅ {model}: {r.choices[0].message.content}")
    except Exception as e:
        print(f"❌ {model}: {str(e)[:120]}")