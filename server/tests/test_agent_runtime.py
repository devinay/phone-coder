import inspect
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest
from pipecat.frames.frames import (
    FunctionCallFromLLM,
    FunctionCallsStartedFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMRunFrame,
)
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.services.llm_service import FunctionCallParams

from agents import AgentRuntime, AgentTurnResetter, build_default_registry


def _make_tool(name: str):
    async def tool(params: FunctionCallParams):
        """Dummy test tool."""
        await params.result_callback(f"called {name}")

    tool.__name__ = name
    tool.__signature__ = inspect.Signature(
        parameters=[
            inspect.Parameter(
                "params",
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                annotation=FunctionCallParams,
            )
        ]
    )
    return tool


@dataclass
class _FakeParams:
    results: list[str]
    context: Any = None
    callbacks: list[Any] | None = None

    async def result_callback(self, result: Any, **kwargs):
        self.results.append(str(result))
        if self.callbacks is not None and kwargs:
            self.callbacks.append(kwargs)


class _FakeTask:
    def __init__(self):
        self.frames = []

    async def queue_frames(self, frames):
        self.frames.extend(frames)


class _FakeRuntime:
    def __init__(self):
        self.return_count = 0

    def return_to_controller_after_worker_turn(self):
        self.return_count += 1


def _all_registry_tools(registry):
    names = sorted({name for spec in registry.all() for name in spec.tool_names})
    return {name: _make_tool(name) for name in names}


def test_default_registry_loads_external_prompts():
    registry = build_default_registry("gpt-4o-mini")

    controller = registry.get("controller")
    doc = registry.get("doc")
    shell = registry.get("shell")
    web = registry.get("web")
    image = registry.get("image")

    assert "ControllerAgent" in controller.prompt_text
    assert "DocAgent" in doc.prompt_text
    assert "activate_agent" in controller.tool_names
    assert "write_to_doc" in doc.tool_names
    assert "run" in shell.activation_hints
    assert "terminal" in shell.activation_hints
    assert doc.may_request == ["diagram", "image", "web"]
    assert web.direct_entry is True
    assert "transparent png" in image.prompt_text.lower()


def test_agent_runtime_switches_prompt_and_visible_tools():
    registry = build_default_registry("gpt-4o-mini")
    runtime = AgentRuntime(registry)
    context = LLMContext(messages=[{"role": "system", "content": "old"}])

    runtime.bind(context=context, task=None, tools=_all_registry_tools(registry))
    assert runtime.active_agent_id == "controller"
    assert "ControllerAgent" in context.messages[0]["content"]

    runtime.apply_agent("shell", user_request="pwd", preserve_context=True)

    assert runtime.active_agent_id == "shell"
    assert "ShellAgent" in context.messages[0]["content"]
    assert context.messages[-1]["content"].endswith("User request: pwd")


def test_agent_runtime_strips_tool_call_messages_when_preserving_context():
    registry = build_default_registry("gpt-4o-mini")
    runtime = AgentRuntime(registry)
    context = LLMContext(
        messages=[
            {"role": "system", "content": "old"},
            {"role": "user", "content": "hello"},
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "function": {"name": "run_command", "arguments": "{}"},
                        "type": "function",
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "done"},
            {"role": "assistant", "content": "finished"},
        ]
    )

    runtime.bind(context=context, task=None, tools=_all_registry_tools(registry))
    runtime.apply_agent("doc", user_request="write notes", preserve_context=True)

    assert [message["role"] for message in context.messages] == [
        "system",
        "user",
        "assistant",
        "user",
    ]
    assert all("tool_calls" not in message for message in context.messages)
    assert all("tool_call_id" not in message for message in context.messages)
    assert context.messages[1]["content"] == "hello"
    assert context.messages[2]["content"] == "finished"


def test_agent_turn_resetter_waits_until_worker_final_text_response():
    runtime = _FakeRuntime()
    resetter = AgentTurnResetter(runtime)

    assert resetter._should_return_to_controller(LLMFullResponseStartFrame()) is False
    assert (
        resetter._should_return_to_controller(
            FunctionCallsStartedFrame(
                function_calls=[
                    FunctionCallFromLLM(
                        function_name="update_diagram",
                        tool_call_id="call_1",
                        arguments={},
                        context=None,
                    )
                ]
            )
        )
        is False
    )
    assert resetter._should_return_to_controller(LLMFullResponseEndFrame()) is False
    assert runtime.return_count == 0

    assert resetter._should_return_to_controller(LLMFullResponseStartFrame()) is False
    assert resetter._should_return_to_controller(LLMFullResponseEndFrame()) is True


@pytest.mark.asyncio
async def test_guard_blocks_tool_not_allowed_for_active_agent():
    registry = build_default_registry("gpt-4o-mini")
    runtime = AgentRuntime(registry)
    context = LLMContext(messages=[{"role": "system", "content": "old"}])
    guarded = runtime.bind(context=context, task=None, tools=_all_registry_tools(registry))
    runtime.apply_agent("shell", preserve_context=True)

    params = _FakeParams(results=[])
    await guarded["read_doc"](params)

    assert params.results
    assert params.results[0].startswith("TOOL_NOT_ALLOWED")


@pytest.mark.asyncio
async def test_worker_agent_cannot_call_controller_admin_tools():
    registry = build_default_registry("gpt-4o-mini")
    runtime = AgentRuntime(registry)
    context = LLMContext(messages=[{"role": "system", "content": "old"}])
    guarded = runtime.bind(context=context, task=None, tools=_all_registry_tools(registry))
    runtime.apply_agent("shell", preserve_context=True)

    params = _FakeParams(
        results=[],
        context=SimpleNamespace(messages=[{"role": "user", "content": "admin reload prompt"}]),
    )
    await guarded["prompt_reload_with_context"](params)

    assert params.results
    assert params.results[0].startswith("TOOL_NOT_ALLOWED")


@pytest.mark.asyncio
async def test_activate_agent_switches_after_context_update_and_queues_llm_run():
    registry = build_default_registry("gpt-4o-mini")
    runtime = AgentRuntime(registry)
    context = LLMContext(messages=[{"role": "system", "content": "old"}])
    task = _FakeTask()
    runtime.bind(context=context, task=task, tools=_all_registry_tools(registry))
    admin_tools = runtime.create_admin_tools()
    callbacks = []
    params = _FakeParams(results=[], context=context, callbacks=callbacks)

    await admin_tools["activate_agent"](
        params,
        agent_id="doc",
        user_request="write project notes",
        reset_context=True,
    )

    assert runtime.active_agent_id == "controller"
    assert callbacks
    on_context_updated = callbacks[0]["properties"].on_context_updated
    await on_context_updated()

    assert runtime.active_agent_id == "doc"
    assert "DocAgent" in context.messages[0]["content"]
    assert context.messages[-1]["content"] == "write project notes"
    assert any(isinstance(frame, LLMRunFrame) for frame in task.frames)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("agent_id", "user_request"),
    [
        ("shell", "search the web for current infra projects"),
        ("doc", "run jq on config.json"),
        ("diagram", "open the existing markdown doc"),
        ("web", "run ls in my home directory"),
    ],
)
async def test_activate_agent_allows_mismatched_request_but_still_handoffs(
    agent_id: str, user_request: str
):
    registry = build_default_registry("gpt-4o-mini")
    runtime = AgentRuntime(registry)
    context = LLMContext(messages=[{"role": "system", "content": "old"}])
    task = _FakeTask()
    runtime.bind(context=context, task=task, tools=_all_registry_tools(registry))
    admin_tools = runtime.create_admin_tools()
    callbacks = []
    params = _FakeParams(results=[], context=context, callbacks=callbacks)

    await admin_tools["activate_agent"](
        params,
        agent_id=agent_id,
        user_request=user_request,
    )

    assert params.results[0].startswith("HANDOFF:")
    assert runtime.active_agent_id == "controller"
    assert not task.frames
    assert callbacks


@pytest.mark.asyncio
async def test_activate_agent_can_force_mismatched_request_for_admin_debug():
    registry = build_default_registry("gpt-4o-mini")
    runtime = AgentRuntime(registry)
    context = LLMContext(
        messages=[
            {"role": "system", "content": "old"},
            {"role": "user", "content": "admin search the web for current infra projects"},
        ]
    )
    task = _FakeTask()
    runtime.bind(context=context, task=task, tools=_all_registry_tools(registry))
    admin_tools = runtime.create_admin_tools()
    callbacks = []
    params = _FakeParams(results=[], context=context, callbacks=callbacks)

    await admin_tools["activate_agent"](
        params,
        agent_id="shell",
        user_request="search the web for current infra projects",
        force=True,
    )

    assert params.results[0].startswith("HANDOFF:")
    assert callbacks


def test_composition_policy_allows_expected_agent_chains():
    registry = build_default_registry("gpt-4o-mini")
    runtime = AgentRuntime(registry)

    assert runtime._check_composition_access(
        target_agent_id="diagram", requested_by_agent_id="doc"
    ) == (True, "doc may request diagram")
    assert runtime._check_composition_access(
        target_agent_id="image", requested_by_agent_id="doc"
    ) == (True, "doc may request image")
    assert runtime._check_composition_access(
        target_agent_id="web", requested_by_agent_id="diagram"
    ) == (True, "diagram may request web")
    assert runtime._check_composition_access(
        target_agent_id="shell", requested_by_agent_id="controller"
    ) == (True, "controller direct entry allowed")


def test_composition_policy_blocks_forbidden_agent_chains():
    registry = build_default_registry("gpt-4o-mini")
    runtime = AgentRuntime(registry)

    assert runtime._check_composition_access(
        target_agent_id="shell", requested_by_agent_id="doc"
    ) == (False, "doc may not request shell")
    assert runtime._check_composition_access(
        target_agent_id="doc", requested_by_agent_id="web"
    ) == (False, "web may not request doc")


@pytest.mark.asyncio
async def test_activate_agent_blocks_forbidden_composition_policy():
    registry = build_default_registry("gpt-4o-mini")
    registry.get("shell").direct_entry = False
    runtime = AgentRuntime(registry)
    context = LLMContext(
        messages=[
            {"role": "system", "content": "old"},
            {"role": "user", "content": "run ls"},
        ]
    )
    task = _FakeTask()
    runtime.bind(context=context, task=task, tools=_all_registry_tools(registry))
    runtime.apply_agent("doc", preserve_context=True)
    runtime.return_to_controller_after_worker_turn()
    admin_tools = runtime.create_admin_tools()
    params = _FakeParams(results=[], context=context, callbacks=[])

    await admin_tools["activate_agent"](
        params,
        agent_id="shell",
        user_request="run ls",
    )

    assert params.results[0].startswith("COMPOSITION_NOT_ALLOWED")


@pytest.mark.asyncio
async def test_activate_agent_allows_composition_policy_chain():
    registry = build_default_registry("gpt-4o-mini")
    runtime = AgentRuntime(registry)
    context = LLMContext(messages=[{"role": "system", "content": "old"}])
    task = _FakeTask()
    runtime.bind(context=context, task=task, tools=_all_registry_tools(registry))
    admin_tools = runtime.create_admin_tools()
    callbacks = []
    params = _FakeParams(results=[], context=context, callbacks=callbacks)

    await admin_tools["activate_agent"](
        params,
        agent_id="diagram",
        user_request="draw a flowchart for these notes",
        requested_by_agent="doc",
    )

    assert params.results[0].startswith("HANDOFF:")
    assert callbacks


@pytest.mark.asyncio
async def test_restricted_agent_requires_runtime_workflow_owner_not_llm_hint():
    registry = build_default_registry("gpt-4o-mini")
    registry.get("image").direct_entry = False
    runtime = AgentRuntime(registry)
    context = LLMContext(
        messages=[
            {"role": "system", "content": "old"},
            {"role": "user", "content": "find me an s3 icon"},
        ]
    )
    task = _FakeTask()
    runtime.bind(context=context, task=task, tools=_all_registry_tools(registry))
    admin_tools = runtime.create_admin_tools()
    params = _FakeParams(results=[], context=context, callbacks=[])

    await admin_tools["activate_agent"](
        params,
        agent_id="image",
        user_request="find me an s3 icon",
        requested_by_agent="doc",
    )

    assert params.results[0].startswith("COMPOSITION_NOT_ALLOWED")


@pytest.mark.asyncio
async def test_restricted_agent_allows_runtime_composition_chain():
    registry = build_default_registry("gpt-4o-mini")
    registry.get("image").direct_entry = False
    runtime = AgentRuntime(registry)
    context = LLMContext(
        messages=[
            {"role": "system", "content": "old"},
            {"role": "user", "content": "find me an s3 icon for the doc"},
        ]
    )
    task = _FakeTask()
    runtime.bind(context=context, task=task, tools=_all_registry_tools(registry))
    runtime.apply_agent("doc", preserve_context=True)
    runtime.return_to_controller_after_worker_turn()
    admin_tools = runtime.create_admin_tools()
    callbacks = []
    params = _FakeParams(results=[], context=context, callbacks=callbacks)

    await admin_tools["activate_agent"](
        params,
        agent_id="image",
        user_request="find me an s3 icon for the doc",
    )

    assert params.results[0].startswith("HANDOFF:")
    assert callbacks


@pytest.mark.asyncio
async def test_force_activation_requires_admin_prefix():
    registry = build_default_registry("gpt-4o-mini")
    registry.get("image").direct_entry = False
    runtime = AgentRuntime(registry)
    context = LLMContext(
        messages=[
            {"role": "system", "content": "old"},
            {"role": "user", "content": "find me an s3 icon"},
        ]
    )
    task = _FakeTask()
    runtime.bind(context=context, task=task, tools=_all_registry_tools(registry))
    admin_tools = runtime.create_admin_tools()

    denied = _FakeParams(results=[], context=context, callbacks=[])
    await admin_tools["activate_agent"](
        denied,
        agent_id="image",
        user_request="find me an s3 icon",
        force=True,
    )
    assert denied.results[0].startswith("ADMIN_REQUIRED")

    context.set_messages(
        [
            {"role": "system", "content": "old"},
            {"role": "user", "content": "admin find me an s3 icon"},
        ]
    )
    allowed = _FakeParams(results=[], context=context, callbacks=[])
    await admin_tools["activate_agent"](
        allowed,
        agent_id="image",
        user_request="find me an s3 icon",
        force=True,
    )
    assert allowed.results[0].startswith("HANDOFF:")


@pytest.mark.asyncio
async def test_prompt_read_requires_admin_prefix():
    registry = build_default_registry("gpt-4o-mini")
    runtime = AgentRuntime(registry)
    admin_tools = runtime.create_admin_tools()

    denied = _FakeParams(
        results=[],
        context=SimpleNamespace(messages=[{"role": "user", "content": "read controller prompt"}]),
    )
    await admin_tools["read_agent_prompt"](denied)
    assert denied.results[0].startswith("ADMIN_REQUIRED")

    allowed = _FakeParams(
        results=[],
        context=SimpleNamespace(messages=[{"role": "user", "content": "admin read controller prompt"}]),
    )
    await admin_tools["read_agent_prompt"](allowed)
    assert "ControllerAgent" in allowed.results[0]


@pytest.mark.asyncio
async def test_admin_prefix_check_skips_synthetic_user_notes():
    registry = build_default_registry("gpt-4o-mini")
    runtime = AgentRuntime(registry)
    admin_tools = runtime.create_admin_tools()

    allowed = _FakeParams(
        results=[],
        context=SimpleNamespace(
            messages=[
                {"role": "user", "content": "Admin: read controller prompt"},
                {"role": "user", "content": "[SYSTEM NOTE] Voice output is now ON"},
                {"role": "user", "content": "[AGENT HANDOFF]\nTarget agent: doc"},
            ]
        ),
    )
    await admin_tools["read_agent_prompt"](allowed)

    assert "ControllerAgent" in allowed.results[0]

    denied = _FakeParams(
        results=[],
        context=SimpleNamespace(
            messages=[
                {"role": "user", "content": "read controller prompt"},
                {"role": "user", "content": "[SYSTEM NOTE] Voice output is now ON"},
            ]
        ),
    )
    await admin_tools["read_agent_prompt"](denied)

    assert denied.results[0].startswith("ADMIN_REQUIRED")


@pytest.mark.asyncio
async def test_prompt_reload_can_switch_allowed_model_with_and_without_context_reset():
    registry = build_default_registry("gpt-4o-mini")
    model_calls = []

    async def switch_model(model: str, preserve_context: bool) -> str:
        model_calls.append((model, preserve_context))
        return f"switched {model}"

    runtime = AgentRuntime(registry)
    context = LLMContext(messages=[{"role": "system", "content": "old"}, {"role": "user", "content": "keep me"}])
    runtime.bind(
        context=context,
        task=None,
        tools=_all_registry_tools(registry),
        model_switcher=switch_model,
    )
    admin_tools = runtime.create_admin_tools()

    keep_context = _FakeParams(
        results=[],
        context=SimpleNamespace(messages=[{"role": "user", "content": "admin reload controller"}]),
    )
    await admin_tools["prompt_reload_with_context"](
        keep_context,
        agent_id="controller",
        model="gpt-4o",
    )

    assert model_calls[-1] == ("gpt-4o", True)
    assert "kept context" in keep_context.results[0]
    assert context.messages[-1]["content"] == "keep me"

    reset_context = _FakeParams(
        results=[],
        context=SimpleNamespace(messages=[{"role": "user", "content": "admin reset controller"}]),
    )
    await admin_tools["prompt_reload_reset_context"](
        reset_context,
        agent_id="controller",
        model="gpt-4.1-mini",
    )

    assert model_calls[-1] == ("gpt-4.1-mini", False)
    assert "reset context" in reset_context.results[0]
    assert len(context.messages) == 1
    assert "ControllerAgent" in context.messages[0]["content"]


@pytest.mark.asyncio
async def test_prompt_reload_rejects_model_outside_agent_allowlist():
    registry = build_default_registry("gpt-4o-mini")
    runtime = AgentRuntime(registry)
    context = LLMContext(messages=[{"role": "system", "content": "old"}])
    runtime.bind(context=context, task=None, tools=_all_registry_tools(registry))
    admin_tools = runtime.create_admin_tools()
    params = _FakeParams(
        results=[],
        context=SimpleNamespace(messages=[{"role": "user", "content": "admin reload controller"}]),
    )

    await admin_tools["prompt_reload_with_context"](
        params,
        agent_id="controller",
        model="not-a-real-model",
    )

    assert params.results[0].startswith("MODEL_NOT_ALLOWED")
