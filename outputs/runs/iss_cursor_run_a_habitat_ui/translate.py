import json
import time
import sys
from deep_translator import GoogleTranslator

INPUT = "messages.jsonl"
OUTPUT = "messages_ja.jsonl"

translator = GoogleTranslator(source='auto', target='ja')

def translate_text(text):
    if not text or not text.strip():
        return text
    # Google Translate limit is 5000 chars; split if needed
    if len(text) <= 4500:
        try:
            return translator.translate(text)
        except Exception as e:
            print(f"  [warn] translate error: {e}", flush=True)
            time.sleep(5)
            try:
                return translator.translate(text)
            except Exception:
                return text
    # Split long text at paragraph breaks
    parts = text.split('\n')
    translated_parts = []
    chunk = ""
    for part in parts:
        if len(chunk) + len(part) + 1 > 4500:
            if chunk:
                try:
                    translated_parts.append(translator.translate(chunk))
                    time.sleep(0.5)
                except Exception:
                    translated_parts.append(chunk)
                chunk = part
            else:
                translated_parts.append(part)
        else:
            chunk = chunk + "\n" + part if chunk else part
    if chunk:
        try:
            translated_parts.append(translator.translate(chunk))
        except Exception:
            translated_parts.append(chunk)
    return "\n".join(translated_parts)

with open(INPUT, "r", encoding="utf-8") as fin, \
     open(OUTPUT, "w", encoding="utf-8") as fout:

    for i, line in enumerate(fin, 1):
        line = line.strip()
        if not line:
            fout.write("\n")
            continue
        obj = json.loads(line)
        original = obj.get("utterance", "")
        if original:
            translated = translate_text(original)
            obj["utterance"] = translated
            time.sleep(0.3)  # rate limiting
        fout.write(json.dumps(obj, ensure_ascii=False) + "\n")

        if i % 100 == 0:
            print(f"  {i}/2655 完了", flush=True)

print("翻訳完了 → messages_ja.jsonl", flush=True)
