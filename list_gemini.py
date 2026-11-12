import os
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()
client = OpenAI(
    base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
    api_key=os.getenv("GEMINI_API_KEY"),
)
for m in sorted(client.models.list(), key=lambda m: m.id):
    if "flash" in m.id:
        print(m.id)