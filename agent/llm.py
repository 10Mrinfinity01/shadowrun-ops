"""One function to talk to any LLM. Tries the provider in LLM_PROVIDER first, then falls back.

Order: chosen provider -> the others (groq, gemini, ollama). Returns (parsed_json | None, provider_used).
"""
import json
import os
import re
import time

PROVIDERS = {
    "groq": {"base_url": "https://api.groq.com/openai/v1", "key": "GROQ_API_KEY", "model": "GROQ_MODEL"},
    "gemini": {"base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
               "key": "GEMINI_API_KEY", "model": "GEMINI_MODEL"},
    "ollama": {"base_url": "http://localhost:11434/v1", "key": None, "model": "OLLAMA_MODEL"},
}


def provider_order(prefer=None):
    first = prefer or os.getenv("LLM_PROVIDER", "groq")
    return [first] + [p for p in PROVIDERS if p != first]


def extract_json(text):
    if not text:
        return None
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)  # some local models print their thinking
    match = re.search(r"\{.*\}", text, flags=re.S)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def chat_json(system, user, max_tokens=1500, timeout=8, prefer=None):
    from openai import OpenAI  # imported here so the rest of the agent works without it in tests

    errors = []
    for name in provider_order(prefer):
        cfg = PROVIDERS[name]
        model = os.getenv(cfg["model"])
        key = os.getenv(cfg["key"]) if cfg["key"] else "ollama"
        if not model or not key:
            errors.append(f"{name}: not configured")
            continue
        extra = {}
        if name == "groq" and "gpt-oss" in model:
            extra["reasoning_effort"] = "low"  # faster, and saves Groq's tokens-per-minute budget
        if name == "gemini":
            extra["reasoning_effort"] = "low"  # otherwise the thinking can use up the whole reply budget
        try:
            started = time.time()
            client = OpenAI(base_url=cfg["base_url"], api_key=key, max_retries=0,
                            timeout=max(timeout, 25) if name == "ollama" else timeout)  # local model: slower
            r = client.chat.completions.create(
                model=model,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                max_tokens=max_tokens,
                temperature=0,
                extra_body=extra or None,
            )
            parsed = extract_json(r.choices[0].message.content)
            if parsed is not None:
                parsed["_latency_s"] = round(time.time() - started, 2)
                return parsed, name
            errors.append(f"{name}: reply was not JSON")
        except Exception as e:
            errors.append(f"{name}: {str(e)[:120]}")
    return None, "; ".join(errors)
