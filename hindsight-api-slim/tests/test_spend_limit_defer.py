"""A used-up spending limit defers work for an hour instead of retrying it.

LiteLLM answers a spent key budget with a 429 of type ``budget_exceeded``, and passes a
DeepInfra account cap through as a 402. Neither names a reset time, so before this fix
the LLM client retried each call four times, the worker retried each task three more
times, and a mental model refresh re-ran its paid synthesis on every attempt.
"""

import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from openai import APIStatusError

from hindsight_api.engine.llm_interface import ProviderRateLimitResetError, is_spend_limit_error
from hindsight_api.engine.memory_engine import _is_non_retryable_task_error
from hindsight_api.engine.providers.openai_compatible_llm import OpenAICompatibleLLM
from hindsight_api.worker.exceptions import DeferOperation, RetryTaskAt

LITELLM_BUDGET_BODY = {
    "error": {
        "message": "Budget has been exceeded! Key=hindsight Current cost: 6.0003, Max budget: 6.0",
        "type": "budget_exceeded",
        "param": None,
        "code": "429",
    }
}
DEEPINFRA_LIMIT_BODY = {
    "error": {
        "message": "litellm.BadRequestError: DeepinfraException - inference prohibited, "
        "you have reached user-set limit. More details: https://deepinfra.com/dash/billing",
        "type": None,
        "param": None,
        "code": "402",
    }
}
EMBEDDING_TOO_LONG_MESSAGE = (
    "Failed to generate batch embeddings: Error code: 400 - {'error': {'message': "
    "'litellm.ContextWindowExceededError: litellm.BadRequestError: ContextWindowExceededError: "
    "OpenAIException - Invalid input[0]: maximum input length is 8192 tokens.'}}"
)


def _status_error(status_code: int, body: dict) -> APIStatusError:
    response = MagicMock()
    response.status_code = status_code
    response.text = json.dumps(body)
    response.headers = {}
    return APIStatusError(f"Error code: {status_code} - {body}", response=response, body=body)


def _wrapped_like_the_embedding_path(error: Exception) -> Exception:
    try:
        raise error
    except Exception as provider_error:
        try:
            raise Exception(f"Failed to generate batch embeddings: {provider_error}")
        except Exception as wrapped:
            return wrapped


class TestSpendLimitDetection:
    def test_litellm_key_budget(self):
        assert is_spend_limit_error(_status_error(429, LITELLM_BUDGET_BODY))

    def test_deepinfra_account_cap(self):
        assert is_spend_limit_error(_status_error(402, DEEPINFRA_LIMIT_BODY))

    def test_found_through_the_embedding_wrapper(self):
        wrapped = _wrapped_like_the_embedding_path(_status_error(429, LITELLM_BUDGET_BODY))
        assert is_spend_limit_error(wrapped)

    def test_ordinary_rate_limit_is_not_a_spend_limit(self):
        body = {"error": {"message": "Rate limit reached", "type": "rate_limit_error"}}
        assert not is_spend_limit_error(_status_error(429, body))


def _llm() -> OpenAICompatibleLLM:
    return OpenAICompatibleLLM(provider="openai", model="deepseek", api_key="test", base_url="https://example.com/v1")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [_status_error(429, LITELLM_BUDGET_BODY), _status_error(402, DEEPINFRA_LIMIT_BODY)],
    ids=["litellm-budget", "deepinfra-cap"],
)
async def test_llm_call_defers_on_the_first_spend_limit_error(error):
    llm = _llm()
    create = AsyncMock(side_effect=error)
    llm._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    with patch("hindsight_api.engine.providers.openai_compatible_llm.asyncio.sleep", new_callable=AsyncMock) as sleep:
        with pytest.raises(ProviderRateLimitResetError, match="Spending limit reached"):
            await llm.call(messages=[{"role": "user", "content": "x"}], scope="reflect", max_retries=3)

    assert create.await_count == 1
    sleep.assert_not_awaited()


async def _pending_refresh(pool) -> dict:
    bank_id = f"test-spend-{uuid.uuid4().hex[:8]}"
    operation_id = uuid.uuid4()
    await pool.execute("INSERT INTO banks (bank_id, name) VALUES ($1, $1) ON CONFLICT DO NOTHING", bank_id)
    task_dict = {
        "type": "refresh_mental_model",
        "operation_id": str(operation_id),
        "bank_id": bank_id,
        "mental_model_id": "mm-1",
        "_retry_count": 0,
    }
    await pool.execute(
        "INSERT INTO async_operations (operation_id, bank_id, operation_type, status, task_payload) "
        "VALUES ($1, $2, 'refresh_mental_model', 'pending', $3::jsonb)",
        operation_id,
        bank_id,
        json.dumps(task_dict),
    )
    return task_dict


@pytest.mark.asyncio
async def test_worker_defers_a_task_that_hit_a_spend_limit(memory):
    pool = await memory._get_pool()
    task_dict = await _pending_refresh(pool)
    spent = _wrapped_like_the_embedding_path(_status_error(429, LITELLM_BUDGET_BODY))

    with patch.object(memory, "_handle_refresh_mental_model", side_effect=spent):
        with pytest.raises(DeferOperation):
            await memory.execute_task(task_dict)

    await pool.execute("DELETE FROM banks WHERE bank_id = $1", task_dict["bank_id"])


@pytest.mark.asyncio
async def test_worker_does_not_retry_an_input_that_is_too_long(memory):
    pool = await memory._get_pool()
    task_dict = await _pending_refresh(pool)
    too_long = Exception(EMBEDDING_TOO_LONG_MESSAGE)

    assert _is_non_retryable_task_error(too_long)
    with patch.object(memory, "_handle_refresh_mental_model", side_effect=too_long):
        try:
            await memory.execute_task(task_dict)
        except RetryTaskAt:
            pytest.fail("an input that is too long was scheduled for retry")
        except Exception:
            pass
    status = await pool.fetchval(
        "SELECT status FROM async_operations WHERE operation_id = $1", uuid.UUID(task_dict["operation_id"])
    )
    assert status == "failed"

    await pool.execute("DELETE FROM banks WHERE bank_id = $1", task_dict["bank_id"])
