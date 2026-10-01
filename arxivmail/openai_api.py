from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from .http import request_json


BASE_URL = "https://api.openai.com/v1"


class OpenAIClient:
    def __init__(self, api_key: str) -> None:
        self.headers = {"Authorization": f"Bearer {api_key}"}

    def embeddings(self, texts: Iterable[str], *, model: str) -> list[list[float]]:
        values = list(texts)
        vectors: list[list[float]] = []
        for start in range(0, len(values), 128):
            batch = values[start : start + 128]
            response, _ = request_json(
                f"{BASE_URL}/embeddings",
                method="POST",
                headers=self.headers,
                payload={"model": model, "input": batch, "encoding_format": "float"},
                timeout=90,
            )
            ordered = sorted(response.get("data", []), key=lambda item: item["index"])
            if len(ordered) != len(batch):
                raise RuntimeError("OpenAI embeddings response did not contain every input")
            vectors.extend([list(map(float, item["embedding"])) for item in ordered])
        return vectors

    def structured_response(
        self,
        *,
        model: str,
        instructions: str,
        input_text: str,
        schema_name: str,
        schema: dict[str, Any],
        reasoning_effort: str = "low",
        max_output_tokens: int = 20000,
    ) -> dict[str, Any]:
        payload = {
            "model": model,
            "instructions": instructions,
            "input": input_text,
            "reasoning": {"effort": reasoning_effort},
            "text": {
                "verbosity": "low",
                "format": {
                    "type": "json_schema",
                    "name": schema_name,
                    "strict": True,
                    "schema": schema,
                },
            },
            "max_output_tokens": max_output_tokens,
            "store": False,
            "prompt_cache_key": "arxivmail-ranking-v1",
        }
        response, _ = request_json(
            f"{BASE_URL}/responses",
            method="POST",
            headers=self.headers,
            payload=payload,
            timeout=180,
            retries=4,
        )
        if response.get("status") not in {None, "completed"}:
            detail = response.get("incomplete_details") or response.get("error") or response.get("status")
            raise RuntimeError(f"OpenAI response did not complete: {detail}")
        output_text = _extract_output_text(response)
        return json.loads(output_text)


def _extract_output_text(response: dict[str, Any]) -> str:
    if isinstance(response.get("output_text"), str):
        return response["output_text"]
    texts: list[str] = []
    for item in response.get("output", []):
        for content in item.get("content", []):
            if content.get("type") == "refusal":
                raise RuntimeError(f"OpenAI refused the ranking request: {content.get('refusal', '')}")
            if content.get("type") == "output_text" and content.get("text"):
                texts.append(content["text"])
    if not texts:
        raise RuntimeError("OpenAI response contained no output text")
    return "".join(texts)

