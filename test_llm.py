import os
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()
providers = {
    "groq":   ("https://api.groq.com/openai/v1", os.getenv("GROQ_API_KEY"), os.getenv("GROQ_MODEL")),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai/", os.getenv("GEMINI_API_KEY"), os.getenv("GEMINI_MODEL")),
    "ollama": ("http://localhost:11434/v1", "ollama", os.getenv("OLLAMA_MODEL")),
}
for name, (url, key, model) in providers.items():
    try:
        r = OpenAI(base_url=url, api_key=key).chat.completions.create(
            model=model, messages=[{"role": "user", "content": "Reply with: OK"}], max_tokens=10)
        print(f"{name}: ✅ {r.choices[0].message.content}")
    except Exception as e:
        print(f"{name}: ❌ {e}")