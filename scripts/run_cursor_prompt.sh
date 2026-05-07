#!/usr/bin/env bash

set -euo pipefail

if ! command -v cursor >/dev/null 2>&1; then
  echo "cursor CLI was not found on PATH." >&2
  exit 127
fi

prompt_from_args="${1:-}"
prompt_from_stdin=""
if [[ -z "${prompt_from_args}" ]]; then
  prompt_from_stdin="$(cat)"
fi
prompt="${prompt_from_args:-$prompt_from_stdin}"

if [[ -z "${prompt}" ]]; then
  echo "No prompt was provided to run_cursor_prompt.sh" >&2
  exit 1
fi

composite_prompt=$(
  cat <<EOF
You are being used as a pure text generation backend inside a simulation loop.
Do not run commands, inspect files, edit the repository, or use tools.
Return only the final answer requested by the prompt below.

$prompt
EOF
)

# Non-interactive runs require workspace trust (otherwise CLI exits with trust prompt).
cursor_cmd=(cursor agent --trust -p --output-format json --mode ask)
if [[ -n "${CURSOR_MODEL:-}" ]]; then
  cursor_cmd+=(--model "$CURSOR_MODEL")
fi

stderr_capture="$(mktemp)"
trap 'rm -f "${stderr_capture}"' EXIT

max_retries="${CURSOR_AGENT_RETRIES:-6}"
retry_sleep_s="${CURSOR_AGENT_RETRY_SLEEP_S:-2}"
attempt_timeout_s="${CURSOR_AGENT_ATTEMPT_TIMEOUT_S:-90}"

# Prevent concurrent `cursor agent` invocations from trampling shared state
# (e.g. ~/.cursor/cli-config.json) and reduce flaky EPROTO bursts.
# Uses an atomic mkdir lock so it works on macOS without flock.
lock_dir="${CURSOR_AGENT_LOCK_DIR:-/tmp/cursor-agent-cli.lock}"
lock_wait_s="${CURSOR_AGENT_LOCK_WAIT_S:-60}"
lock_sleep_s="${CURSOR_AGENT_LOCK_SLEEP_S:-0.2}"

acquire_lock() {
  local start
  start="$(date +%s)"
  while ! mkdir "${lock_dir}" 2>/dev/null; do
    # Best-effort stale lock cleanup (e.g. crash). Ignore failures.
    if [[ -f "${lock_dir}/pid" ]]; then
      local pid
      pid="$(cat "${lock_dir}/pid" 2>/dev/null || true)"
      if [[ -n "${pid}" ]] && ! kill -0 "${pid}" 2>/dev/null; then
        rm -rf "${lock_dir}" 2>/dev/null || true
        continue
      fi
    fi
    if (( $(date +%s) - start >= lock_wait_s )); then
      echo "cursor agent lock timeout after ${lock_wait_s}s: ${lock_dir}" >&2
      return 1
    fi
    sleep "${lock_sleep_s}"
  done
  echo "$$" > "${lock_dir}/pid" 2>/dev/null || true
  return 0
}

release_lock() {
  rm -rf "${lock_dir}" 2>/dev/null || true
}

set +e
raw_json=""
cursor_exit=1
for attempt in $(seq 1 "${max_retries}"); do
  : >"${stderr_capture}"
  if ! acquire_lock; then
    cursor_exit=1
  else
    # Run with a per-attempt timeout so a hung CLI call can't stall the whole simulation.
    # We delegate to Python for timeout handling to keep this script portable on macOS.
    raw_json="$(
      python3 - "$attempt_timeout_s" "$composite_prompt" "${cursor_cmd[@]}" 2>"${stderr_capture}" <<'PY'
import subprocess
import sys

timeout_s = float(sys.argv[1])
prompt = sys.argv[2]
cmd = sys.argv[3:]

try:
    completed = subprocess.run(
        [*cmd, prompt],
        capture_output=True,
        text=True,
        timeout=timeout_s,
        check=False,
    )
except subprocess.TimeoutExpired:
    print(f"cursor agent timed out after {timeout_s}s.", file=sys.stderr)
    sys.exit(124)

if completed.stderr:
    sys.stderr.write(completed.stderr)

sys.stdout.write(completed.stdout or "")
sys.exit(completed.returncode)
PY
    )"
    cursor_exit=$?
    release_lock
  fi
  if [[ "${cursor_exit}" -eq 0 ]]; then
    break
  fi

  # Retry known transient Cursor CLI failures:
  # - OpenSSL EPROTO
  # - cli-config.json.tmp rename ENOENT
  if grep -qiE 'write EPROTO|tls_get_more_records|cli-config\.json\.tmp|rename .*cli-config\.json' "${stderr_capture}"; then
    # Exponential backoff with small jitter.
    # (Keeps pressure off the CLI backend when it gets into a bad state.)
    base_sleep="${retry_sleep_s}"
    if [[ "${base_sleep}" == "0" ]]; then
      base_sleep="1"
    fi
    # attempt=1 -> 1x, attempt=2 -> 2x, attempt=3 -> 4x ...
    sleep_for=$(( base_sleep * (2 ** (attempt - 1)) ))
    # jitter: 0..300ms
    jitter_ms=$(( RANDOM % 300 ))
    python3 - <<PY "${sleep_for}" "${jitter_ms}"
import sys, time
sleep_for = float(sys.argv[1])
jitter_ms = int(sys.argv[2])
time.sleep(sleep_for + (jitter_ms / 1000.0))
PY
    continue
  fi

  break
done
set -e

if [[ "${cursor_exit}" -ne 0 ]]; then
  echo "cursor agent failed (exit ${cursor_exit})." >&2
  if [[ -s "${stderr_capture}" ]]; then
    cat "${stderr_capture}" >&2
  fi
  exit 1
fi

if [[ -z "${raw_json}" ]]; then
  echo "cursor agent returned empty stdout." >&2
  if [[ -s "${stderr_capture}" ]]; then
    cat "${stderr_capture}" >&2
  fi
  exit 1
fi

python3 - <<'PY' "$raw_json"
import json
import sys

raw = sys.argv[1]

def extract_text(payload):
    if isinstance(payload, str):
        return payload.strip()
    if isinstance(payload, dict):
        for key in ("result", "response", "output_text", "text", "content", "message"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        # Common nested shapes
        if isinstance(payload.get("message"), dict):
            maybe = payload["message"].get("content")
            if isinstance(maybe, str) and maybe.strip():
                return maybe.strip()
        if isinstance(payload.get("data"), dict):
            nested = extract_text(payload["data"])
            if nested:
                return nested
        return json.dumps(payload, ensure_ascii=False)
    if isinstance(payload, list):
        for item in payload:
            extracted = extract_text(item)
            if extracted:
                return extracted
        return ""
    return str(payload).strip()

try:
    parsed = json.loads(raw)
except json.JSONDecodeError:
    print(raw.strip())
    sys.exit(0)

result = extract_text(parsed)
if not result:
    preview = raw if len(raw) <= 800 else raw[:800] + "..."
    print(
        "cursor JSON parsed but extract_text returned empty. Raw preview:\n" + preview,
        file=sys.stderr,
    )
    sys.exit(1)
print(result)
PY
