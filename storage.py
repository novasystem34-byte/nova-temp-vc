"""
وحدة بسيطة للتعامل مع ملفات JSON (قراءة وحفظ) بشكل آمن.
"""
import json
import os
import threading

_lock = threading.Lock()


def load_json(path: str) -> dict:
    """تحميل بيانات JSON من ملف، وإرجاع قاموس فارغ إذا لم يكن الملف موجوداً أو تالفاً."""
    if not os.path.exists(path):
        return {}
    with _lock:
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read().strip()
                if not content:
                    return {}
                return json.loads(content)
        except (json.JSONDecodeError, FileNotFoundError):
            return {}


def save_json(path: str, data: dict) -> None:
    """حفظ قاموس بايثون كملف JSON مرتب."""
    with _lock:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
