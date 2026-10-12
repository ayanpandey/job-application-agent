"""
Unified LLM Client supporting Google Gemini on Vertex AI with Global Location,
automatic OAuth2 token refresh & lifecycle management via Google Cloud Service Account.
"""

import os
import json
import re
import datetime
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from src.core.config import LLMConfig
from src.core.logger import logger


class LLMClient:
    def __init__(self, config: LLMConfig):
        self.config = config
        self.provider = config.provider.lower()
        self._creds = None
        self._auth_request = None
        # No hardcoded project fallback. A shipped binary must not carry the
        # operator's GCP project id, and in "proxy" mode -- which is what every
        # customer build runs -- neither of these is consulted at all.
        self.project_id = os.getenv("GCP_PROJECT_ID", "")
        self.location = os.getenv("GCP_LOCATION") or "global"
        self.client = None
        self.api_base = ""
        # Both are read by the desktop UI: why AI is degraded right now, and how
        # much of the licence's daily allowance the last call left behind.
        self.last_error: Optional[str] = None
        self.last_quota: Dict[str, Any] = {}
        # Evaluations now run in worker threads so the browser can keep moving
        # during the round-trip. The HTTP calls themselves are thread-safe, but
        # token refresh *rebuilds* self.client, so two threads refreshing at once
        # could have one of them issue a request against a half-swapped client.
        self._auth_lock = threading.Lock()
        self._init_provider()

    def _init_provider(self):
        if self.provider == "proxy":
            # Nothing to construct. Every call becomes an authenticated HTTP
            # request to the licence proxy, which holds both the Google
            # credentials and the prompt templates -- so a customer machine has
            # neither, and losing the binary costs nothing but a revoked key.
            from src.core import licensing

            self.api_base = licensing.api_base_url()
            # Sentinel rather than an SDK object: the rest of this class reads
            # `self.client` only as "is a provider configured at all".
            self.client = "proxy"
            if not licensing.get_license_key():
                logger.warning(
                    "[yellow]No licence key stored yet — AI features stay off until one is entered.[/yellow]"
                )
            return

        if self.provider == "gemini":
            from google import genai
            from google.oauth2 import service_account
            from google.auth.transport.requests import Request

            # Environment variable only -- there is deliberately no default path
            # here any more. The old default of ./config/service_account.json is
            # precisely what caused a live Google private key to be picked up by
            # the packaging step and shipped inside a customer build.
            sa_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "")
            sa_file = Path(sa_path).resolve() if sa_path else None

            if sa_file and sa_file.exists():
                logger.info(f"[green]✓ Authenticating Gemini via Google Cloud Service Account ({sa_file.name})[/green]")
                try:
                    self._creds = service_account.Credentials.from_service_account_file(
                        str(sa_file),
                        scopes=[
                            "https://www.googleapis.com/auth/cloud-platform",
                            "https://www.googleapis.com/auth/generative-language"
                        ]
                    )
                    self._auth_request = Request()
                    # Generate initial token
                    self._creds.refresh(self._auth_request)
                    self.project_id = self._creds.project_id or self.project_id
                    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(sa_file)
                    
                    self.client = genai.Client(
                        vertexai=True,
                        project=self.project_id,
                        location=self.location,
                        credentials=self._creds
                    )
                    logger.info(f"[dim green]Connected to Vertex AI Gemini on project '{self.project_id}' in region '{self.location}'. Token expires at {self._creds.expiry} (Auto-refresh enabled).[/dim green]")
                    return
                except Exception as sa_err:
                    logger.warning(f"Failed to init Vertex AI with service account: {sa_err}")

            if self.config.api_key:
                logger.info("[green]✓ Authenticating Gemini via API Key[/green]")
                self.client = genai.Client(api_key=self.config.api_key)
            else:
                logger.warning("[yellow]No Gemini API key or service account found. LLM running in heuristic mode.[/yellow]")
                self.client = None

        elif self.provider in ("openai", "groq"):
            from openai import OpenAI
            base_url = "https://api.groq.com/openai/v1" if self.provider == "groq" else None
            if not self.config.api_key:
                logger.warning(f"[yellow]{self.provider.upper()}_API_KEY is not set.[/yellow]")
                self.client = None
            else:
                self.client = OpenAI(api_key=self.config.api_key, base_url=base_url)

        elif self.provider == "ollama":
            from openai import OpenAI
            self.client = OpenAI(
                base_url=f"{self.config.ollama_base_url}/v1",
                api_key="ollama"
            )
        else:
            self.client = None

    def _ensure_active_token(self):
        """Automatically refreshes OAuth2 token if expired or close to expiry."""
        if self._creds and self._auth_request:
            try:
                with self._auth_lock:
                    # Proactive refresh if token is missing, expired, or expiring in next 2 minutes
                    is_expired = not self._creds.valid or self._creds.expired
                    if is_expired:
                        logger.info("[yellow]OAuth2 token expired. Automatically generating a fresh API access token...[/yellow]")
                        self._creds.refresh(self._auth_request)
                        from google import genai
                        self.client = genai.Client(
                            vertexai=True,
                            project=self.project_id,
                            location=self.location,
                            credentials=self._creds
                        )
                        logger.info(f"[green]✓ Fresh API token generated successfully! New Expiry: {self._creds.expiry}[/green]")
            except Exception as e:
                logger.error(f"Error during automatic token refresh: {e}")

    def _gemini_config(self, json_mode: bool):
        """
        Generation settings for Gemini.

        Two things here are latency, not preference. `thinking_budget=0` turns off
        the reasoning pass, which the 3.x flash models run by default and which
        adds seconds to every call -- the work in this agent is extraction and
        scoring against an explicit rubric, not multi-step reasoning, so it buys
        nothing. `response_mime_type` makes the model emit JSON directly instead
        of a fenced block that has to be regex-repaired downstream.

        Returns None when the installed SDK does not expose these, so the caller
        falls back to a plain unconfigured call rather than failing.
        """
        try:
            from google.genai import types
        except Exception:
            return None

        kwargs: Dict[str, Any] = {
            "temperature": self.config.temperature,
            "max_output_tokens": self.config.max_tokens,
        }
        if json_mode:
            kwargs["response_mime_type"] = "application/json"
        try:
            kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=0)
        except Exception:
            pass

        try:
            return types.GenerateContentConfig(**kwargs)
        except Exception:
            kwargs.pop("thinking_config", None)
            try:
                return types.GenerateContentConfig(**kwargs)
            except Exception:
                return None

    def generate(self, system_prompt: str, user_prompt: str, json_mode: bool = False) -> str:
        """Generate plain text completion with automatic token refresh."""
        self._ensure_active_token()

        if not self.client:
            return self._fallback_response(user_prompt)

        if self.provider == "proxy":
            # There is no branch below for "proxy" -- a caller that assembled
            # its own prompt cannot be served by a service that only accepts
            # named tasks. This used to fall through the whole if/elif chain and
            # return None, which the JSON parser turned into {} and callers took
            # for an empty-but-valid answer; the resume engine did exactly that
            # on every run of every shipped build. Fail loudly instead.
            raise RuntimeError(
                "generate() is not available in proxy mode. Use run_task()/"
                "run_task_json() with a task name registered on the server."
            )

        try:
            if self.provider == "gemini":
                # Primary model: gemini-3.7-flash (with seamless fallback)
                model_name = self.config.model if self.config.model else "gemini-3.7-flash"
                gen_config = self._gemini_config(json_mode)
                try:
                    response = self.client.models.generate_content(
                        model=model_name,
                        contents=f"{system_prompt}\n\n{user_prompt}",
                        config=gen_config,
                    )
                    return (response.text or "").strip()
                except Exception as model_err:
                    if model_name != "gemini-2.5-flash":
                        logger.debug(f"Retrying with fallback model gemini-2.5-flash: {model_err}")
                        response = self.client.models.generate_content(
                            model="gemini-2.5-flash",
                            contents=f"{system_prompt}\n\n{user_prompt}",
                            config=gen_config,
                        )
                        return (response.text or "").strip()
                    raise model_err

            elif self.provider in ("openai", "groq", "ollama"):
                kwargs: Dict[str, Any] = {
                    "model": self.config.model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "temperature": self.config.temperature,
                    "max_tokens": self.config.max_tokens,
                }
                if json_mode:
                    kwargs["response_format"] = {"type": "json_object"}
                try:
                    response = self.client.chat.completions.create(**kwargs)
                except Exception:
                    # Not every OpenAI-compatible endpoint (Ollama in particular)
                    # accepts response_format. Retry without it before giving up.
                    kwargs.pop("response_format", None)
                    response = self.client.chat.completions.create(**kwargs)
                return response.choices[0].message.content.strip()

        except Exception as e:
            logger.error(f"Error invoking LLM ({self.provider}): {e}")
            # If error might be due to auth, attempt immediate token refresh and retry once
            if self._creds and ("401" in str(e) or "403" in str(e) or "unauthenticated" in str(e).lower()):
                try:
                    logger.info("Attempting automatic re-auth retry...")
                    self._creds.refresh(self._auth_request)
                    from google import genai
                    self.client = genai.Client(
                        vertexai=True,
                        project=self.project_id,
                        location=self.location,
                        credentials=self._creds
                    )
                    model_name = self.config.model or "gemini-2.5-flash"
                    response = self.client.models.generate_content(
                        model=model_name,
                        contents=f"{system_prompt}\n\n{user_prompt}",
                        config=self._gemini_config(json_mode),
                    )
                    return (response.text or "").strip()
                except Exception as retry_err:
                    logger.error(f"Retry also failed: {retry_err}")

            return self._fallback_response(user_prompt)

    def generate_json(self, system_prompt: str, user_prompt: str) -> Dict[str, Any]:
        """Generate structured JSON response with regex cleanup."""
        return self._parse_json(self.generate(system_prompt, user_prompt, json_mode=True))

    # -- task interface ------------------------------------------------------
    #
    # Callers ask for a *task* and hand over variables; they never assemble a
    # prompt. In proxy mode the templates live on the server and the client
    # genuinely cannot reconstruct them. In direct mode (development, or a
    # bring-your-own-key build) the same task is resolved against a local
    # prompts.yaml, so both paths behave identically from the call site.

    _TASK_SECTIONS = {
        "job_matcher": ("job_matcher",),
        "question_solver": ("question_solver",),
        "question_solver_batch": ("question_solver", "batch"),
        "recruiter_outreach": ("recruiter_outreach",),
        "resume_tailor": ("resume_tailor",),
        "resume_audit": ("resume_audit",),
        "keyword_expansion": ("keyword_expansion",),
        "profile_extract": ("profile_extract",),
        "element_resolver": ("element_resolver",),
    }

    def run_task(
        self,
        task: str,
        variables: Dict[str, Any],
        json_mode: bool = True,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> str:
        if self.provider == "proxy":
            return self._run_task_proxy(task, variables, json_mode, temperature, max_tokens)
        return self._run_task_local(task, variables, json_mode)

    def run_task_json(self, task: str, variables: Dict[str, Any]) -> Dict[str, Any]:
        raw = self.run_task(task, variables, json_mode=True)
        return self._parse_json(raw)

    def _run_task_proxy(
        self,
        task: str,
        variables: Dict[str, Any],
        json_mode: bool,
        temperature: Optional[float],
        max_tokens: Optional[int],
    ) -> str:
        import httpx

        from src.core import licensing

        key = licensing.get_license_key()
        if not key:
            self.last_error = "No licence key stored."
            return self._fallback_response(str(variables))

        payload = {
            "task": task,
            "variables": variables,
            "json_mode": json_mode,
            "temperature": self.config.temperature if temperature is None else temperature,
            "max_tokens": self.config.max_tokens if max_tokens is None else max_tokens,
            "device_id": licensing.device_id(),
        }
        headers = {"Authorization": f"Bearer {key}"}

        # Two attempts. The first covers a cold Cloud Run instance or a dropped
        # connection; beyond that the caller's own fallback is the better answer
        # than making a browser session sit and wait.
        last_exc: Optional[Exception] = None
        for attempt in (1, 2):
            try:
                response = httpx.post(
                    f"{self.api_base}/v1/task", json=payload, headers=headers, timeout=90.0
                )
            except Exception as exc:
                last_exc = exc
                continue

            if response.status_code == 200:
                body = response.json()
                self.last_quota = body.get("quota") or {}
                self.last_error = None
                return (body.get("text") or "").strip()

            # 4xx is a decision by the server -- a bad key, an exhausted quota,
            # a revoked licence. Retrying cannot change it, and the message is
            # written to be shown to the customer verbatim.
            detail = {}
            try:
                detail = response.json().get("detail", {}) or {}
            except Exception:
                pass
            message = detail.get("message") or response.text[:200]
            code = detail.get("code", f"http_{response.status_code}")
            if 400 <= response.status_code < 500:
                self.last_error = message
                logger.error(f"[red]Licence problem ({code}): {message}[/red]")
                return self._fallback_response(str(variables))
            last_exc = RuntimeError(f"{code}: {message}")

        self.last_error = f"Could not reach the AI service: {last_exc}"
        logger.error(f"[red]{self.last_error}[/red]")
        return self._fallback_response(str(variables))

    def _run_task_local(self, task: str, variables: Dict[str, Any], json_mode: bool) -> str:
        """Development path: resolve the task against a local prompts.yaml."""
        from src.core.config import load_prompts

        path = self._TASK_SECTIONS.get(task)
        if path is None:
            logger.error(f"Unknown LLM task '{task}'.")
            return self._fallback_response(str(variables))

        node: Any = load_prompts()
        for part in path:
            node = (node or {}).get(part, {})
        section = node or {}

        system_prompt = section.get("system", "")
        user_template = section.get("user_template", "")
        if not user_template:
            logger.error(f"prompts.yaml has no template for task '{task}'.")
            return self._fallback_response(str(variables))

        class _Defaulting(dict):
            def __missing__(self, key: str) -> str:
                return "(not provided)"

        safe = _Defaulting({k: str(v) for k, v in variables.items()})
        user_prompt = user_template.format_map(safe)
        if system_prompt:
            system_prompt = system_prompt.format_map(safe)
        return self.generate(system_prompt, user_prompt, json_mode=json_mode)

    @staticmethod
    def _parse_json(raw_text: str) -> Dict[str, Any]:
        clean_text = re.sub(r"^```(?:json)?\s*", "", (raw_text or "").strip(), flags=re.MULTILINE)
        clean_text = re.sub(r"\s*```$", "", clean_text.strip(), flags=re.MULTILINE)
        try:
            return json.loads(clean_text)
        except json.JSONDecodeError:
            match = re.search(r"(\{.*\})", clean_text, re.DOTALL)
            if match:
                try:
                    return json.loads(match.group(1))
                except Exception:
                    pass
            logger.warning(f"Failed to parse LLM JSON output. Raw response: {(raw_text or '')[:100]}...")
            return {}

    def _fallback_response(self, user_prompt: str) -> str:
        """
        What to say when there is no model available.

        Both branches here used to answer confidently and affirmatively: every
        job came back "Strong Fit, 85" and every screening question came back
        "Yes". An unreachable API therefore looked exactly like a very successful
        session, and a run with no credentials would apply to everything it saw
        and answer every sponsorship question wrong on the way.

        Both now signal unavailability instead, and the callers fall back to
        deterministic scoring (matcher) or escalate to a human (form solver).
        """
        if "match_score" in user_prompt:
            return json.dumps({
                "llm_unavailable": True,
                "summary_reasoning": "LLM unavailable; scored from deterministic signals only.",
            })
        if "confidence" in user_prompt:
            return json.dumps({
                "llm_unavailable": True,
                "answer": "",
                "confidence": 0.0,
                "reasoning": "LLM unavailable; leaving this question for a human.",
            })
        if "char_count" in user_prompt:
            # No note here, deliberately. This used to answer with a generic
            # one-liner ("Hi! I saw the opening on your team..."), which was not
            # wrong so much as *shadowing*: OutreachGenerator has its own
            # fallback that names the recruiter, the role, the company and the
            # candidate's own pitch, and it only runs `if not note`. A non-empty
            # note here meant that better fallback could never run -- in exactly
            # the case it was written for. Signal unavailability and let the
            # caller build the good one.
            return json.dumps({
                "llm_unavailable": True,
                "note": "",
                "char_count": 0,
            })
        # Not a shape this method knows. Returning prose would be parsed as {}
        # by `_parse_json`, which callers read as "no opinion" -- correct, but
        # only by accident. Say it in the shape every other branch uses.
        return json.dumps({"llm_unavailable": True})
