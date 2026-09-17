"""Provider-neutral reasoning contract and OpenAI-compatible reference adapter."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from pydantic import ValidationError

from ai_data_platform.reasoning.models import CuratedReasoningContext, ReasoningDraft


class ReasoningProviderError(RuntimeError):
    """Raised when a provider cannot return a valid structured draft."""


class DatasetReasoningProvider(Protocol):
    @property
    def provider_id(self) -> str: ...

    def generate(self, context: CuratedReasoningContext) -> ReasoningDraft: ...


Transport = Callable[[str, dict[str, str], bytes, float], bytes]


SYSTEM_INSTRUCTIONS = """You select and explain a dataset using only the supplied context.
Never answer the analytical question and never generate SQL or transformation guidance.
Use only dataset IDs listed in candidates and only evidence IDs listed in evidence_catalog.
When selection_allowed is true, select exactly one candidate and ground the explanation in evidence.
When selection_allowed is false, selected_dataset_id must be null. Explain the ambiguity and ask
one concise clarification question without making a tentative recommendation.
Return only the requested structured JSON object."""


def reasoning_output_schema() -> dict[str, object]:
    return {
        "type": "object",
        "properties": {
            "selected_dataset_id": {"type": ["string", "null"]},
            "explanation": {"type": "string"},
            "evidence_ids": {"type": "array", "items": {"type": "string"}},
            "clarification_question": {"type": ["string", "null"]},
        },
        "required": [
            "selected_dataset_id",
            "explanation",
            "evidence_ids",
            "clarification_question",
        ],
        "additionalProperties": False,
    }


class OpenAIResponsesReasoningProvider:
    """Call an OpenAI-compatible Responses API with strict structured output."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = "https://api.openai.com/v1",
        timeout_seconds: float = 30.0,
        transport: Transport | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("api_key must not be empty")
        if not model.strip():
            raise ValueError("model must not be empty")
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._transport = transport or self._send

    @property
    def provider_id(self) -> str:
        return f"openai-responses:{self.model}"

    def _send(
        self,
        url: str,
        headers: dict[str, str],
        body: bytes,
        timeout_seconds: float,
    ) -> bytes:
        request = Request(url, data=body, headers=headers, method="POST")
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                return response.read()
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise ReasoningProviderError(
                f"reasoning provider returned HTTP {error.code}: {detail}"
            ) from error
        except URLError as error:
            raise ReasoningProviderError(f"reasoning provider request failed: {error.reason}") from error

    def build_payload(self, context: CuratedReasoningContext) -> dict[str, object]:
        return {
            "model": self.model,
            "input": [
                {"role": "system", "content": SYSTEM_INSTRUCTIONS},
                {
                    "role": "user",
                    "content": json.dumps(context.model_dump(mode="json"), sort_keys=True),
                },
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "dataset_selection",
                    "strict": True,
                    "schema": reasoning_output_schema(),
                }
            },
            "max_output_tokens": 800,
            "store": False,
        }

    @staticmethod
    def _extract_output_text(response: dict[str, object]) -> str:
        direct = response.get("output_text")
        if isinstance(direct, str) and direct:
            return direct
        output = response.get("output")
        if isinstance(output, list):
            for item in output:
                if not isinstance(item, dict):
                    continue
                content = item.get("content")
                if not isinstance(content, list):
                    continue
                for part in content:
                    if (
                        isinstance(part, dict)
                        and part.get("type") == "output_text"
                        and isinstance(part.get("text"), str)
                    ):
                        return part["text"]
        raise ReasoningProviderError("reasoning provider response did not contain output text")

    def generate(self, context: CuratedReasoningContext) -> ReasoningDraft:
        payload = self.build_payload(context)
        raw = self._transport(
            f"{self.base_url}/responses",
            {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json.dumps(payload).encode("utf-8"),
            self.timeout_seconds,
        )
        try:
            response = json.loads(raw.decode("utf-8"))
            if not isinstance(response, dict):
                raise TypeError("expected a JSON object")
            return ReasoningDraft.model_validate_json(self._extract_output_text(response))
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValidationError) as error:
            raise ReasoningProviderError(f"invalid structured reasoning response: {error}") from error

