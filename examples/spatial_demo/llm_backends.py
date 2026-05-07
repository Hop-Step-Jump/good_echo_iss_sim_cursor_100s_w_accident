"""
LLM backend factory and command-based client implementations.
"""
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import time
from typing import Any, Dict, List, Optional, Protocol, Sequence, Tuple, Union, runtime_checkable

from ollama_client import OllamaClient

logger = logging.getLogger(__name__)

PROMPT_PLACEHOLDER = "{prompt}"
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


@runtime_checkable
class LLMClientProtocol(Protocol):
    """Common interface expected by the simulation."""

    def generate(
        self,
        prompt: str,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None
    ) -> str:
        """Generate a response for the given prompt."""

    def check_connection(self) -> bool:
        """Return whether the backend is reachable or runnable."""

    def check_model_exists(self) -> bool:
        """Return whether the configured model exists."""

    def list_models(self) -> List[str]:
        """List available models when supported."""


class CommandLLMClient:
    """Run an external CLI as the LLM backend."""

    def __init__(
        self,
        command: Union[Sequence[str], str],
        response_format: str = "text",
        response_json_field: Optional[str] = None,
        timeout_seconds: int = 180,
        prompt_mode: str = "auto",
        env: Optional[Dict[str, str]] = None,
        working_directory: Optional[str] = None,
        model: Optional[str] = None,
        max_retries: int = 0,
        retry_backoff_seconds: float = 0.0,
        start_new_session: bool = False,
        stdout_filter_regex: Optional[Sequence[str]] = None,
    ):
        self.command = self._normalize_command(command)
        self.response_format = response_format
        self.response_json_field = response_json_field
        self.timeout_seconds = timeout_seconds
        self.prompt_mode = prompt_mode
        self.env = env or {}
        self.working_directory = working_directory
        self.model = model
        self.max_retries = max(0, int(max_retries))
        self.retry_backoff_seconds = max(0.0, float(retry_backoff_seconds))
        self.start_new_session = bool(start_new_session)
        self.stdout_filter_regexes: List[re.Pattern] = [
            re.compile(pattern) for pattern in (stdout_filter_regex or [])
        ]

    def _normalize_command(self, command: Union[Sequence[str], str]) -> List[str]:
        """Normalize the configured command into an argv list."""
        if isinstance(command, str):
            return shlex.split(command)
        return [str(part) for part in command]

    def _build_invocation(self, prompt: str) -> Tuple[List[str], Optional[str]]:
        """Build the command argv and optional stdin input."""
        if not self.command:
            raise ValueError("CLI backend requires a non-empty 'llm.command' setting.")

        has_placeholder = any(PROMPT_PLACEHOLDER in part for part in self.command)

        if self.prompt_mode not in {"auto", "stdin", "append_arg"}:
            raise ValueError(
                "llm.prompt_mode must be one of: auto, stdin, append_arg"
            )

        if has_placeholder:
            argv = [part.replace(PROMPT_PLACEHOLDER, prompt) for part in self.command]
            return argv, None

        if self.prompt_mode == "stdin":
            return list(self.command), prompt

        return [*self.command, prompt], None

    def _extract_json_field(self, payload: Any) -> str:
        """Extract a nested field from a parsed JSON payload."""
        if self.response_json_field:
            current = payload
            for part in self.response_json_field.split("."):
                if not isinstance(current, dict) or part not in current:
                    raise KeyError(
                        f"response_json_field '{self.response_json_field}' not found"
                    )
                current = current[part]
            if isinstance(current, str):
                return current.strip()
            return json.dumps(current, ensure_ascii=False)

        for key in ("result", "output_text", "text", "content"):
            if isinstance(payload, dict) and isinstance(payload.get(key), str):
                return payload[key].strip()

        if isinstance(payload, str):
            return payload.strip()

        return json.dumps(payload, ensure_ascii=False)

    def _clean_output(self, text: str) -> str:
        """Remove ANSI escapes, configured noise lines, and surrounding whitespace."""
        without_ansi = ANSI_ESCAPE_RE.sub("", text)
        if not self.stdout_filter_regexes:
            return without_ansi.strip()
        kept: List[str] = []
        for line in without_ansi.splitlines():
            if any(pattern.search(line) for pattern in self.stdout_filter_regexes):
                continue
            kept.append(line)
        return "\n".join(kept).strip()

    def _preview_output(self, text: str, limit: int = 600) -> str:
        """Return a single-line preview for logs."""
        normalized = text.replace("\n", "\\n")
        if len(normalized) <= limit:
            return normalized
        return normalized[:limit] + "...<truncated>"

    def _sleep_before_retry(self, attempt: int) -> None:
        """Sleep with a simple linear backoff before retrying."""
        if self.retry_backoff_seconds <= 0:
            return
        time.sleep(self.retry_backoff_seconds * attempt)

    def generate(
        self,
        prompt: str,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None
    ) -> str:
        """Generate text by spawning the configured command."""
        del temperature
        del max_tokens

        total_attempts = self.max_retries + 1
        for attempt in range(1, total_attempts + 1):
            try:
                argv, stdin_input = self._build_invocation(prompt)
                run_env = os.environ.copy()
                run_env.update(self.env)
                completed = subprocess.run(
                    argv,
                    input=stdin_input,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_seconds,
                    cwd=self.working_directory,
                    env=run_env,
                    check=False,
                    start_new_session=self.start_new_session and os.name == "posix",
                )
            except FileNotFoundError:
                logger.error(
                    "Configured CLI backend executable was not found: %s",
                    self.command[0]
                )
                return ""
            except subprocess.TimeoutExpired:
                if attempt < total_attempts:
                    logger.warning(
                        "CLI backend timed out after %s seconds on attempt %s/%s: %s",
                        self.timeout_seconds,
                        attempt,
                        total_attempts,
                        " ".join(self.command),
                    )
                    self._sleep_before_retry(attempt)
                    continue
                logger.error(
                    "CLI backend timed out after %s seconds: %s",
                    self.timeout_seconds,
                    " ".join(self.command),
                )
                return ""
            except Exception as e:
                if attempt < total_attempts:
                    logger.warning(
                        "Unexpected CLI backend error on attempt %s/%s: %s",
                        attempt,
                        total_attempts,
                        e,
                    )
                    self._sleep_before_retry(attempt)
                    continue
                logger.error("Unexpected error while running CLI backend: %s", e)
                return ""

            stdout = self._clean_output(completed.stdout)
            stderr = self._clean_output(completed.stderr)

            if completed.returncode != 0:
                detail = self._preview_output(stderr or stdout or "(no output)")
                if attempt < total_attempts:
                    logger.warning(
                        "CLI backend exited with code %s on attempt %s/%s: %s",
                        completed.returncode,
                        attempt,
                        total_attempts,
                        detail,
                    )
                    self._sleep_before_retry(attempt)
                    continue
                logger.error(
                    "CLI backend exited with code %s: %s",
                    completed.returncode,
                    detail,
                )
                return ""

            if not stdout:
                if attempt < total_attempts:
                    logger.warning(
                        "CLI backend returned no stdout output on attempt %s/%s.",
                        attempt,
                        total_attempts,
                    )
                    self._sleep_before_retry(attempt)
                    continue
                logger.warning("CLI backend returned no stdout output.")
                return ""

            if self.response_format == "json":
                try:
                    payload = json.loads(stdout)
                    return self._extract_json_field(payload)
                except Exception as e:
                    preview = self._preview_output(stdout)
                    if attempt < total_attempts:
                        logger.warning(
                            "Unexpected JSON-shaped CLI output on attempt %s/%s: %s; stdout=%s",
                            attempt,
                            total_attempts,
                            e,
                            preview,
                        )
                        self._sleep_before_retry(attempt)
                        continue
                    logger.error(
                        "Failed to parse JSON from CLI backend output: %s; stdout=%s",
                        e,
                        preview,
                    )
                    return ""

            return stdout

        return ""

    def check_connection(self) -> bool:
        """Check whether the configured executable is available."""
        executable = self.command[0] if self.command else ""
        return bool(executable) and shutil.which(executable) is not None

    def check_model_exists(self) -> bool:
        """Command-backed clients cannot usually validate model availability upfront."""
        return True

    def list_models(self) -> List[str]:
        """Return the configured model when one was supplied."""
        return [self.model] if self.model else []


def create_llm_client(llm_config: Dict[str, Any]) -> LLMClientProtocol:
    """Create an LLM client from config."""
    provider = llm_config.get("provider", "ollama").lower()

    if provider == "ollama":
        return OllamaClient(
            base_url=llm_config["base_url"],
            model=llm_config["model"],
            temperature=llm_config.get("temperature", 0.7),
            max_tokens=llm_config.get("max_tokens", 200),
            repeat_penalty=llm_config.get("repeat_penalty", 1.1),
            repeat_last_n=llm_config.get("repeat_last_n", 128),
            min_p=llm_config.get("min_p", 0.05),
        )

    if provider in {"command", "cli"}:
        env = {
            str(key): str(value)
            for key, value in llm_config.get("env", {}).items()
        }
        return CommandLLMClient(
            command=llm_config["command"],
            response_format=llm_config.get("response_format", "text"),
            response_json_field=llm_config.get("response_json_field"),
            timeout_seconds=int(llm_config.get("timeout_seconds", 180)),
            prompt_mode=llm_config.get("prompt_mode", "auto"),
            env=env,
            working_directory=llm_config.get("working_directory"),
            model=llm_config.get("model"),
            max_retries=int(llm_config.get("max_retries", 0)),
            retry_backoff_seconds=float(llm_config.get("retry_backoff_seconds", 0.0)),
            start_new_session=bool(llm_config.get("start_new_session", False)),
            stdout_filter_regex=llm_config.get("stdout_filter_regex"),
        )

    raise ValueError(f"Unsupported llm.provider: '{provider}'")
