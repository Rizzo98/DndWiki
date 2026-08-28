"""RefinerLLM tests: parse leniency + corrective retry (fake litellm)."""

import sys
import types
from types import SimpleNamespace

import pytest

from app.core.config import ServiceSettings
from app.llm import RefinerLLM
from app.refine import RefineError


def make_settings(**overrides):
    kwargs = {
        "refiner_json_retries": 1,
        "refiner_temperature": 0.0,
        "refiner_max_tokens": 100,
        "refiner_model": "",
        "llm_model": "deepseek/deepseek-chat",
        "deepseek_api_key": "test-key",
    }
    kwargs.update(overrides)
    return ServiceSettings(**kwargs)


def install_fake_litellm(monkeypatch, responses):
    module = types.ModuleType("litellm")
    module.drop_params = True
    calls = []

    async def acompletion(**kwargs):
        calls.append(kwargs)
        content = responses.pop(0)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
        )

    module.acompletion = acompletion
    module._calls = calls
    monkeypatch.setitem(sys.modules, "litellm", module)
    return module


def good_json():
    return '{"turns": [{"index": 0, "speaker": "SPEAKER_01", "text": "fixed line"}]}'


def fenced_json():
    fence = chr(96) * 3
    return fence + "json\n" + '{"turns": [{"index": 0, "speaker": "SPEAKER_00", "text": "ok",}]}' + "\n" + fence


async def test_refine_window_parses_valid_json(monkeypatch):
    install_fake_litellm(monkeypatch, [good_json()])
    llm = RefinerLLM(make_settings())
    decisions = await llm.refine_window(
        [{"index": 0, "text": "raw"}],
        context_blocks=[],
        fixed_count=0,
        window_index=0,
        total_windows=1,
    )
    assert decisions == {0: ("SPEAKER_01", "fixed line")}


async def test_refine_window_repairs_slightly_malformed_json(monkeypatch):
    install_fake_litellm(monkeypatch, [fenced_json()])
    llm = RefinerLLM(make_settings())
    decisions = await llm.refine_window(
        [{"index": 0, "text": "raw"}],
        context_blocks=[],
        fixed_count=0,
        window_index=0,
        total_windows=1,
    )
    assert decisions == {0: ("SPEAKER_00", "ok")}


async def test_refine_window_corrective_retry(monkeypatch):
    fake = install_fake_litellm(monkeypatch, ["not json at all", good_json()])
    llm = RefinerLLM(make_settings(refiner_json_retries=1))
    decisions = await llm.refine_window(
        [{"index": 0, "text": "raw"}],
        context_blocks=[],
        fixed_count=0,
        window_index=0,
        total_windows=1,
    )
    assert decisions == {0: ("SPEAKER_01", "fixed line")}
    assert len(fake._calls) == 2


async def test_refine_window_fails_after_retries(monkeypatch):
    install_fake_litellm(monkeypatch, ["nope", "still nope"])
    llm = RefinerLLM(make_settings(refiner_json_retries=1))
    with pytest.raises(RefineError):
        await llm.refine_window(
            [{"index": 0, "text": "raw"}],
            context_blocks=[],
            fixed_count=0,
            window_index=0,
            total_windows=1,
        )


async def test_refine_window_empty_response(monkeypatch):
    install_fake_litellm(monkeypatch, [""])
    llm = RefinerLLM(make_settings(refiner_json_retries=0))
    with pytest.raises(RefineError, match="empty"):
        await llm.refine_window(
            [{"index": 0, "text": "raw"}],
            context_blocks=[],
            fixed_count=0,
            window_index=0,
            total_windows=1,
        )


def test_load_json_rejects_plain_prose():
    llm = RefinerLLM(make_settings())
    with pytest.raises(RefineError, match="unparseable"):
        llm._load_json("This is not JSON at all.")