"""Log sanitizer: runs before any log text reaches the LLM.

1. Redacts secrets (passwords, API keys, tokens, credentials inside URLs).
2. Flags lines that look like instructions aimed at an AI (prompt injection through logs).
3. Wraps everything in markers so the model treats it as data, never as orders.
"""
import re

SECRET_PATTERNS = [
    (re.compile(r"(postgres(?:ql)?://[^:\s/]+:)([^@\s]+)(@)"), r"\1[REDACTED]\3"),
    (re.compile(r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key)\s*[=:]\s*\S+"), r"\1=[REDACTED]"),
    (re.compile(r"\bgsk_[A-Za-z0-9]{10,}"), "[REDACTED_GROQ_KEY]"),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}"), "[REDACTED_GOOGLE_KEY]"),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"), "[REDACTED_KEY]"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{10,}"), "Bearer [REDACTED]"),
    (re.compile(r"\b\d{6,}:[A-Za-z0-9_\-]{30,}"), "[REDACTED_BOT_TOKEN]"),
]

INJECTION_PATTERNS = [
    r"ignore (all |any )?(previous|prior|above) instructions",
    r"\b(system|developer) (prompt|instruction|message)\b",
    r"\bAI agent\b",
    r"\byou (must|should) now\b",
    r"\bdrop\s+(database|table)\b",
    r"\brm\s+-rf\b",
    r"\bdocker\s+(rm|volume\s+rm|system\s+prune)\b",
    r"\bdelete (the )?(database|volume|all)\b",
    r"\bdisable (the )?(safety|policy|guard)",
]
INJECTION_RE = re.compile("|".join(INJECTION_PATTERNS), re.I)
# Upper-case only: real logs say "system: 0.00 s" (e.g. Postgres timing lines), attacks shout "SYSTEM:".
SHOUTED_SYSTEM_RE = re.compile(r"\bSYSTEM\s*:")


def redact(line):
    for pattern, repl in SECRET_PATTERNS:
        line = pattern.sub(repl, line)
    return line


def sanitize_logs(lines, source):
    """Returns (wrapped_text_for_llm, suspicious_lines)."""
    clean, suspicious = [], []
    for raw in lines:
        line = redact(raw)[:220]
        if INJECTION_RE.search(line) or SHOUTED_SYSTEM_RE.search(line):
            suspicious.append(f"[{source}] {line}")
            line = "[QUARANTINED: line contained instructions aimed at an AI; withheld]"
        clean.append(line)
    body = "\n".join(clean)
    wrapped = (f'<untrusted_log source="{source}">\n{body}\n</untrusted_log>')
    return wrapped, suspicious
