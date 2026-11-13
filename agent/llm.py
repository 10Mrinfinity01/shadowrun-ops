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
    """The first JSON object in a reply. Copes with extra text, code fences, or a model that writes
    two objects in a row (e.g. a tool call followed by a guessed result)."""
    if not text:
        return None
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)  # some local models print their thinking
    decoder = json.JSONDecoder()
    for m in re.finditer(r"\{", text):
        try:
            obj, _ = decoder.raw_decode(text[m.start():])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def _from_tool_calls(message):
    """Models with built-in tool calling (e.g. gpt-oss) may answer with a native tool call and no text.
    Turn that into the same JSON our agent expects."""
    calls = getattr(message, "tool_calls", None) or []
    for c in calls:
        fn = getattr(c, "function", None)
        if not fn:
            continue
        try:
            args = json.loads(fn.arguments or "{}")
        except Exception:
            args = {}
        if fn.name in ("final", "final_answer", "answer"):
            return {"thought": "", "final": args.get("final", args)}
        return {"thought": args.pop("thought", ""), "tool": fn.name, "args": args.get("args", args)}
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
        client = OpenAI(base_url=cfg["base_url"], api_key=key, max_retries=0,
                        timeout=max(timeout, 25) if name == "ollama" else timeout)  # local model: slower
        # JSON mode makes the model answer with one JSON object; if a provider refuses it, ask again without.
        for json_mode in (True, False):
            try:
                started = time.time()
                r = client.chat.completions.create(
                    model=model,
                    messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                    max_tokens=max_tokens,
                    temperature=0,
                    extra_body=extra or None,
                    **({"response_format": {"type": "json_object"}} if json_mode else {}),
                )
                message = r.choices[0].message
                content = message.content or ""
                parsed = extract_json(content) or _from_tool_calls(message)
                usage = getattr(r, "usage", None)
                tokens = getattr(usage, "total_tokens", None) if usage else None
                if not tokens:  # some providers don't report usage: estimate (about 4 characters per token)
                    tokens = (len(system) + len(user) + len(content)) // 4
                if parsed is not None:
                    parsed["_latency_s"] = round(time.time() - started, 2)
                    parsed["_tokens"] = int(tokens)
                    return parsed, name
                errors.append(f"{name}: reply was not JSON ({content[:60]!r})")
                break
            except Exception as e:
                text = str(e)
                if json_mode and ("response_format" in text or "json" in text.lower()) and "timed out" not in text.lower():
                    continue  # this provider/model doesn't do JSON mode: retry once without it
                errors.append(f"{name}: {text[:120]}")
                break
    return None, "; ".join(errors)
