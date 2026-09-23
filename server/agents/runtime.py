"""Small agent registry/runtime layered over the existing Pipecat pipeline."""

from __future__ import annotations

import inspect
import re
import time
from dataclasses import dataclass, field
from functools import wraps
from pathlib import Path
from typing import Any, Awaitable, Callable

import yaml
from loguru import logger
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.frames.frames import (
    FunctionCallResultProperties,
    FunctionCallsStartedFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMRunFrame,
)
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.services.llm_service import FunctionCallParams

from helpers import message_dict

PromptModelSwitcher = Callable[[str, bool], Awaitable[str]]


@dataclass
class AgentSpec:
    """Static agent definition validated against prompt frontmatter."""

    agent_id: str
    prompt_path: Path
    tool_names: list[str]
    default_model: str
    allowed_models: list[str]
    context_policy: str = "preserve"
    activation_hints: list[str] = field(default_factory=list)
    direct_entry: bool = True
    may_request: list[str] = field(default_factory=list)
    prompt_text: str = ""
    frontmatter: dict[str, Any] = field(default_factory=dict)


class AgentRegistry:
    """Loads prompt files and exposes validated agent definitions."""

    def __init__(
        self,
        specs: list[AgentSpec],
        prompt_suffix: str = "",
        agent_models: dict[str, str] | None = None,
    ):
        self._specs = {spec.agent_id: spec for spec in specs}
        # Appended to every agent prompt. Lives here rather than in the initial
        # system message so it survives context resets and prompt reloads, both
        # of which rebuild the system message from spec.prompt_text.
        self._prompt_suffix = prompt_suffix
        # Deployment-level per-agent model choices. Applied after the prompt is
        # loaded so configuration wins over whatever the prompt declares.
        self._agent_models = agent_models or {}
        for agent_id in list(self._specs):
            self.reload_prompt(agent_id)

    def get(self, agent_id: str) -> AgentSpec:
        try:
            return self._specs[agent_id]
        except KeyError as exc:
            raise ValueError(f"Unknown agent '{agent_id}'") from exc

    def all(self) -> list[AgentSpec]:
        return [self._specs[k] for k in sorted(self._specs)]

    def reload_prompt(self, agent_id: str) -> AgentSpec:
        spec = self.get(agent_id)
        frontmatter, body = _load_prompt_file(spec.prompt_path)
        declared_agent_id = frontmatter.get("agent_id")
        if declared_agent_id and declared_agent_id != spec.agent_id:
            raise ValueError(
                f"Prompt {spec.prompt_path} declares agent_id={declared_agent_id!r}, "
                f"expected {spec.agent_id!r}"
            )

        declared_tools = frontmatter.get("tools") or []
        if declared_tools:
            unknown = sorted(set(declared_tools) - set(spec.tool_names))
            if unknown:
                raise ValueError(
                    f"Prompt {spec.prompt_path} declares tools not allowed by registry: {unknown}"
                )

        declared_models = frontmatter.get("allowed_models") or []
        if declared_models:
            unknown_models = sorted(set(declared_models) - set(spec.allowed_models))
            if unknown_models:
                raise ValueError(
                    f"Prompt {spec.prompt_path} declares models not allowed by registry: "
                    f"{unknown_models}"
                )

        declared_hints = frontmatter.get("activation_hints") or []
        if declared_hints and (
            not isinstance(declared_hints, list)
            or not all(isinstance(item, str) and item.strip() for item in declared_hints)
        ):
            raise ValueError(
                f"Prompt {spec.prompt_path} activation_hints must be a list of non-empty strings"
            )

        declared_policy = frontmatter.get("composition_policy") or {}
        if declared_policy and not isinstance(declared_policy, dict):
            raise ValueError(
                f"Prompt {spec.prompt_path} composition_policy must be a mapping"
            )
        declared_direct_entry = declared_policy.get("direct_entry", spec.direct_entry)
        if not isinstance(declared_direct_entry, bool):
            raise ValueError(
                f"Prompt {spec.prompt_path} composition_policy.direct_entry must be a boolean"
            )
        declared_may_request = declared_policy.get("may_request") or []
        if declared_may_request and (
            not isinstance(declared_may_request, list)
            or not all(isinstance(item, str) and item.strip() for item in declared_may_request)
        ):
            raise ValueError(
                f"Prompt {spec.prompt_path} composition_policy.may_request must be a list of non-empty strings"
            )
        unknown_requested_agents = sorted(
            set(declared_may_request) - set(self._specs) - {spec.agent_id}
        )
        if unknown_requested_agents:
            raise ValueError(
                f"Prompt {spec.prompt_path} composition_policy references unknown agents: "
                f"{unknown_requested_agents}"
            )

        # A prompt may declare the model it wants; the registry's own default is
        # the fallback. Validated against the same allow-list as an explicit
        # switch, so a prompt cannot smuggle in a disallowed model.
        declared_default_model = frontmatter.get("default_model")
        if declared_default_model:
            if declared_default_model not in spec.allowed_models:
                raise ValueError(
                    f"Prompt {spec.prompt_path} declares default_model="
                    f"{declared_default_model!r}, which is not in allowed_models"
                )
            spec.default_model = declared_default_model

        # Configuration overrides the prompt's declaration.
        configured_model = self._agent_models.get(spec.agent_id)
        if configured_model:
            spec.default_model = configured_model

        spec.frontmatter = frontmatter
        spec.activation_hints = [item.strip() for item in declared_hints]
        spec.direct_entry = declared_direct_entry
        spec.may_request = [item.strip() for item in declared_may_request]
        spec.prompt_text = body.strip() + "\n" + self._prompt_suffix
        logger.info(
            f"[AGENT PROMPT] loaded agent={spec.agent_id} path={spec.prompt_path} "
            f"chars={len(spec.prompt_text)} tools={','.join(spec.tool_names)} "
            f"default_model={spec.default_model} hints={','.join(spec.activation_hints) or '<none>'} "
            f"direct_entry={spec.direct_entry} may_request={','.join(spec.may_request) or '<none>'}"
        )
        return spec


class AgentRuntime:
    """Owns active agent state, prompt reload, tools, and execution guards."""

    def __init__(self, registry: AgentRegistry, controller_agent_id: str = "controller"):
        self.registry = registry
        self.controller_agent_id = controller_agent_id
        self.active_agent_id = controller_agent_id
        self._context: Any | None = None
        self._task: Any | None = None
        self._tools: dict[str, Any] = {}
        self._guarded_tools: dict[str, Any] = {}
        self._model_switcher: PromptModelSwitcher | None = None
        self._pending_workflow_owner_agent_id: str | None = None
        self._pending_workflow_user_message: str | None = None
        # Model the pipeline is currently on, so agent handoffs only pay for a
        # switch when the target actually wants a different one.
        self._active_model: str | None = None

    @property
    def active_spec(self) -> AgentSpec:
        return self.registry.get(self.active_agent_id)

    async def apply_agent_model(self, agent_id: str, *, preserve_context: bool = True) -> str:
        """Switch the pipeline to the model this agent declares, if it differs."""
        spec = self.registry.get(agent_id)
        target = spec.default_model
        if not target or not self._model_switcher:
            return ""
        if target == self._active_model:
            return ""
        logger.info(
            f"[AGENT MODEL] agent={agent_id} model={self._active_model or '<unset>'}→{target}"
        )
        result = await self._model_switcher(target, preserve_context)
        self._active_model = target
        return result

    def bind(
        self,
        *,
        context: Any,
        task: Any,
        tools: dict[str, Any],
        model_switcher: PromptModelSwitcher | None = None,
    ) -> dict[str, Any]:
        self._context = context
        self._task = task
        self._tools = tools
        self._model_switcher = model_switcher
        self._guarded_tools = {
            name: self._guard_tool(name, func)
            for name, func in tools.items()
        }
        logger.info(
            f"[AGENT] runtime bound controller={self.controller_agent_id} "
            f"registered_tools={len(tools)} agents={','.join(s.agent_id for s in self.registry.all())}"
        )
        self.apply_agent(self.controller_agent_id, preserve_context=True)
        return self._guarded_tools

    def create_admin_tools(self) -> dict[str, Any]:
        runtime = self

        async def list_agents(params: FunctionCallParams):
            """List available agents, active agent, prompt path, and default model."""
            logger.info(f"[AGENT ADMIN] list_agents active={runtime.active_agent_id}")
            lines = [f"Active agent: {runtime.active_agent_id}", "", "Agents:"]
            for spec in runtime.registry.all():
                lines.append(
                    f"- {spec.agent_id}: model={spec.default_model}, "
                    f"context={spec.context_policy}, hints={', '.join(spec.activation_hints) or '<none>'}, "
                    f"direct_entry={spec.direct_entry}, may_request={', '.join(spec.may_request) or '<none>'}, "
                    f"prompt={spec.prompt_path}"
                )
            await params.result_callback("\n".join(lines))

        async def list_agent_tools(params: FunctionCallParams, agent_id: str = ""):
            """List tools available to an agent.

            Args:
                agent_id: Agent id. Leave empty for the active agent.
            """
            spec = runtime.registry.get(agent_id or runtime.active_agent_id)
            logger.info(
                f"[AGENT ADMIN] list_agent_tools requested={agent_id or runtime.active_agent_id} "
                f"tools={','.join(spec.tool_names)}"
            )
            await params.result_callback(
                f"Tools for {spec.agent_id}:\n" + "\n".join(f"- {name}" for name in spec.tool_names)
            )

        async def read_agent_prompt(params: FunctionCallParams, agent_id: str = ""):
            """Read an agent prompt file for debugging.

            Args:
                agent_id: Agent id. Leave empty for the active agent.
            """
            if not _admin_authorized(params):
                logger.warning(
                    f"[AGENT ADMIN] denied read_agent_prompt agent={agent_id or runtime.active_agent_id}"
                )
                await params.result_callback(
                    "ADMIN_REQUIRED: Prompt reads require the latest user request to start "
                    "with 'admin '."
                )
                return
            spec = runtime.registry.get(agent_id or runtime.active_agent_id)
            logger.info(
                f"[AGENT ADMIN] read_agent_prompt agent={spec.agent_id} path={spec.prompt_path}"
            )
            await params.result_callback(
                f"Prompt for {spec.agent_id} ({spec.prompt_path}):\n\n{spec.prompt_text}"
            )

        async def activate_agent(
            params: FunctionCallParams,
            agent_id: str,
            user_request: str,
            reset_context: bool = False,
            force: bool = False,
            requested_by_agent: str = "",
        ):
            """Activate a worker agent for the user's request.

            Args:
                agent_id: Target agent id, such as shell, doc, diagram, or web.
                user_request: The user's request, copied verbatim or summarized only when needed.
                reset_context: If true, reset the target agent context before running.
                force: Admin-only override for composition checks during debug flows.
                requested_by_agent: Optional workflow owner hint for logging only. Runtime
                    enforcement derives the workflow owner from the last completed worker turn.
            """
            spec = runtime.registry.get(agent_id)
            source_agent = runtime.active_agent_id
            requested_by_hint = requested_by_agent.strip()
            requested_by = runtime._effective_requested_by_agent()
            if requested_by_hint:
                runtime.registry.get(requested_by_hint)
                if requested_by_hint != requested_by:
                    logger.warning(
                        f"[AGENT COMPOSE] ignored requested_by_hint={requested_by_hint} "
                        f"effective_requested_by={requested_by} target={agent_id}"
                    )

            if force and not _admin_authorized(params):
                logger.warning(
                    f"[AGENT ADMIN] denied activate_agent force target={agent_id} "
                    f"requested_by={requested_by} hint={requested_by_hint or '<none>'}"
                )
                await params.result_callback(
                    "ADMIN_REQUIRED: Forced agent activation requires the latest user request "
                    "to start with 'admin '."
                )
                return

            if not force:
                allowed, reason = runtime._check_composition_access(
                    target_agent_id=agent_id,
                    requested_by_agent_id=requested_by,
                )
                logger.info(
                    f"[AGENT COMPOSE] requested_by={requested_by} target={agent_id} "
                    f"allowed={allowed} reason={reason}"
                )
                if not allowed:
                    await params.result_callback(
                        f"COMPOSITION_NOT_ALLOWED: {reason}"
                    )
                    return
            else:
                logger.warning(
                    f"[AGENT COMPOSE] force override requested_by={requested_by} target={agent_id}"
                )

            hint_match = True
            if (
                agent_id != runtime.controller_agent_id
                and user_request
                and spec.activation_hints
            ):
                hint_match = _request_matches_activation_hints(user_request, spec.activation_hints)
                if not hint_match:
                    logger.warning(
                        f"[AGENT HANDOFF] hint_mismatch source={source_agent} target={agent_id} "
                        f"force={force} request={_truncate(user_request)} "
                        f"hints={','.join(spec.activation_hints)}"
                    )
            logger.info(
                f"[AGENT HANDOFF] requested source={source_agent} target={agent_id} "
                f"requested_by={requested_by} reset_context={reset_context} "
                f"force={force} hint_match={hint_match} "
                f"request={_truncate(user_request)}"
            )

            async def _switch_and_run():
                logger.info(
                    f"[AGENT HANDOFF] applying source={source_agent} target={agent_id} "
                    f"requested_by={requested_by} reset_context={reset_context}"
                )
                runtime._clear_pending_workflow_owner()
                runtime.apply_agent(
                    agent_id,
                    user_request=user_request,
                    preserve_context=not reset_context,
                    source_agent=source_agent,
                )
                # Each agent declares the model it should run on; without this
                # the declaration was inert and every agent inherited whatever
                # the controller happened to be using.
                await runtime.apply_agent_model(agent_id, preserve_context=not reset_context)
                if runtime._task:
                    logger.info(f"[AGENT HANDOFF] queue_llm_run target={agent_id}")
                    await runtime._task.queue_frames([LLMRunFrame()])

            await params.result_callback(
                f"HANDOFF: activating {agent_id} for: {user_request}",
                properties=FunctionCallResultProperties(
                    run_llm=False,
                    on_context_updated=_switch_and_run,
                ),
            )

        async def prompt_reload_with_context(
            params: FunctionCallParams,
            agent_id: str = "",
            model: str = "",
        ):
            """Reload an agent prompt while preserving context.

            Args:
                agent_id: Agent id. Leave empty for the active agent.
                model: Optional model id to switch to if allowed for the agent.
            """
            if not _admin_authorized(params):
                logger.warning(
                    f"[AGENT ADMIN] denied prompt_reload_with_context "
                    f"agent={agent_id or runtime.active_agent_id} model={model or '<unchanged>'}"
                )
                await params.result_callback(
                    "ADMIN_REQUIRED: Prompt reload requires the latest user request to start "
                    "with 'admin '."
                )
                return
            target = agent_id or runtime.active_agent_id
            logger.info(
                f"[AGENT ADMIN] prompt_reload_with_context agent={target} "
                f"model={model or '<unchanged>'}"
            )
            msg = await runtime.reload_prompt(target, reset_context=False, model=model or None)
            await params.result_callback(msg)

        async def prompt_reload_reset_context(
            params: FunctionCallParams,
            agent_id: str = "",
            model: str = "",
        ):
            """Reload an agent prompt and reset that agent's context.

            Args:
                agent_id: Agent id. Leave empty for the active agent.
                model: Optional model id to switch to if allowed for the agent.
            """
            if not _admin_authorized(params):
                logger.warning(
                    f"[AGENT ADMIN] denied prompt_reload_reset_context "
                    f"agent={agent_id or runtime.active_agent_id} model={model or '<unchanged>'}"
                )
                await params.result_callback(
                    "ADMIN_REQUIRED: Prompt reload requires the latest user request to start "
                    "with 'admin '."
                )
                return
            target = agent_id or runtime.active_agent_id
            logger.info(
                f"[AGENT ADMIN] prompt_reload_reset_context agent={target} "
                f"model={model or '<unchanged>'}"
            )
            msg = await runtime.reload_prompt(target, reset_context=True, model=model or None)
            await params.result_callback(msg)

        return {
            "list_agents": list_agents,
            "list_agent_tools": list_agent_tools,
            "read_agent_prompt": read_agent_prompt,
            "activate_agent": activate_agent,
            "prompt_reload_with_context": prompt_reload_with_context,
            "prompt_reload_reset_context": prompt_reload_reset_context,
        }

    def apply_agent(
        self,
        agent_id: str,
        *,
        user_request: str = "",
        preserve_context: bool = True,
        source_agent: str = "",
    ) -> None:
        if self._context is None:
            raise RuntimeError("AgentRuntime is not bound to an LLMContext")
        spec = self.registry.get(agent_id)
        previous_agent = self.active_agent_id
        self.active_agent_id = agent_id

        if preserve_context and getattr(self._context, "messages", None):
            messages = _strip_tool_call_messages(self._context.messages)
            first = message_dict(messages[0]) if messages else None
            if first and first.get("role") == "system":
                messages[0] = {"role": "system", "content": spec.prompt_text}
            else:
                messages.insert(0, {"role": "system", "content": spec.prompt_text})
            if user_request:
                handoff = (
                    f"[AGENT HANDOFF]\n"
                    f"Target agent: {agent_id}\n"
                    f"Source: {source_agent or 'controller'}\n"
                    f"User request: {user_request}"
                )
                messages.append({"role": "user", "content": handoff})
        else:
            messages = [{"role": "system", "content": spec.prompt_text}]
            if user_request:
                messages.append({"role": "user", "content": user_request})

        self._context.set_messages(messages)
        self._context.set_tools(ToolsSchema(self._tools_for_agent(spec)))
        logger.info(
            f"[AGENT SWITCH] {previous_agent}→{agent_id} "
            f"preserve_context={preserve_context} message_count={len(messages)} "
            f"model={spec.default_model} context={spec.context_policy} "
            f"prompt={spec.prompt_path} tools={','.join(spec.tool_names)} "
            f"request={_truncate(user_request) if user_request else '<none>'}"
        )

    async def reload_prompt(
        self,
        agent_id: str,
        *,
        reset_context: bool,
        model: str | None = None,
    ) -> str:
        logger.info(
            f"[AGENT PROMPT] reload_start agent={agent_id} reset_context={reset_context} "
            f"model={model or '<unchanged>'}"
        )
        spec = self.registry.reload_prompt(agent_id)
        model_msg = ""
        if model:
            if model not in spec.allowed_models:
                logger.warning(
                    f"[AGENT PROMPT] reload_model_denied agent={agent_id} model={model} "
                    f"allowed={','.join(spec.allowed_models)}"
                )
                return (
                    f"MODEL_NOT_ALLOWED: {model} is not allowed for {agent_id}. "
                    f"Allowed: {', '.join(spec.allowed_models)}"
                )
            spec.default_model = model
            if self._model_switcher:
                model_msg = "\n" + await self._model_switcher(model, not reset_context)

        if agent_id == self.active_agent_id:
            self.apply_agent(agent_id, preserve_context=not reset_context)
        logger.info(
            f"[AGENT PROMPT] reload_done agent={agent_id} reset_context={reset_context} "
            f"model={spec.default_model} active={self.active_agent_id}"
        )
        return (
            f"OK: Reloaded prompt for {agent_id} "
            f"({'reset context' if reset_context else 'kept context'})."
            f"{model_msg}"
        )

    def agent_for_tool(self, tool_name: str) -> str:
        """The agent that can use a tool, preferring the one already active.

        Lets a caller that knows what it needs done — the terminal monitor needs
        ``send_keys`` — ask for capability rather than hard-coding "shell", so a
        registry change cannot silently strand it on an agent without hands.
        """
        if tool_name in self.active_spec.tool_names:
            return self.active_agent_id
        for spec in self.registry.all():
            if tool_name in spec.tool_names and spec.agent_id != self.controller_agent_id:
                return spec.agent_id
        return ""

    def return_to_controller_after_worker_turn(self) -> None:
        if self.active_agent_id == self.controller_agent_id:
            return
        previous = self.active_agent_id
        latest_user_message = self._latest_real_user_message()
        self.apply_agent(self.controller_agent_id, preserve_context=True)
        self._pending_workflow_owner_agent_id = previous
        self._pending_workflow_user_message = latest_user_message
        logger.info(f"[AGENT] completed worker turn; {previous} → controller")

    def _tools_for_agent(self, spec: AgentSpec) -> list[Any]:
        missing = [name for name in spec.tool_names if name not in self._guarded_tools]
        if missing:
            raise ValueError(f"Agent {spec.agent_id} references unknown tools: {missing}")
        return [self._guarded_tools[name] for name in spec.tool_names]

    def _check_composition_access(
        self,
        *,
        target_agent_id: str,
        requested_by_agent_id: str,
    ) -> tuple[bool, str]:
        target = self.registry.get(target_agent_id)
        if requested_by_agent_id == self.controller_agent_id:
            if target.direct_entry:
                return True, "controller direct entry allowed"
            return False, f"{target_agent_id} is not a direct-entry agent"

        requester = self.registry.get(requested_by_agent_id)
        if target_agent_id in requester.may_request:
            return True, f"{requested_by_agent_id} may request {target_agent_id}"
        return False, f"{requested_by_agent_id} may not request {target_agent_id}"

    def _effective_requested_by_agent(self) -> str:
        latest_user_message = self._latest_real_user_message()
        if (
            self._pending_workflow_owner_agent_id
            and latest_user_message
            and latest_user_message == self._pending_workflow_user_message
        ):
            return self._pending_workflow_owner_agent_id
        return self.controller_agent_id

    def _clear_pending_workflow_owner(self) -> None:
        self._pending_workflow_owner_agent_id = None
        self._pending_workflow_user_message = None

    def _latest_real_user_message(self) -> str:
        messages = getattr(self._context, "messages", []) or []
        for message in reversed(messages):
            entry = message_dict(message)
            if entry is None or entry.get("role") != "user":
                continue
            content = entry.get("content", "")
            if isinstance(content, str):
                normalized = content.strip()
                if _is_synthetic_user_note(normalized.lower()):
                    continue
                return normalized
        return ""

    def _guard_tool(self, tool_name: str, func: Any) -> Any:
        @wraps(func)
        async def guarded(params: FunctionCallParams, *args: Any, **kwargs: Any):
            active = self.active_spec
            call_id = _truncate(getattr(params, "tool_call_id", ""), limit=16) or "no-call-id"
            arguments = dict(getattr(params, "arguments", {}) or {})
            if not arguments:
                arguments = dict(kwargs)
            if tool_name not in active.tool_names:
                logger.warning(
                    f"[AGENT TOOL] blocked call_id={call_id} agent={active.agent_id} "
                    f"tool={tool_name} allowed={active.tool_names} args={_format_args(arguments)}"
                )
                await params.result_callback(
                    f"TOOL_NOT_ALLOWED: '{tool_name}' is not available to "
                    f"agent '{active.agent_id}'. Ask the controller to route to the right agent."
                )
                return
            started = time.monotonic()
            logger.info(
                f"[AGENT TOOL] start call_id={call_id} agent={active.agent_id} "
                f"tool={tool_name} args={_format_args(arguments)}"
            )
            try:
                result = await func(params, *args, **kwargs)
            except Exception as exc:
                elapsed_ms = int((time.monotonic() - started) * 1000)
                logger.exception(
                    f"[AGENT TOOL] error call_id={call_id} agent={active.agent_id} "
                    f"tool={tool_name} elapsed_ms={elapsed_ms} error={exc}"
                )
                raise
            elapsed_ms = int((time.monotonic() - started) * 1000)
            logger.info(
                f"[AGENT TOOL] done call_id={call_id} agent={active.agent_id} "
                f"tool={tool_name} elapsed_ms={elapsed_ms}"
            )
            return result

        guarded.__signature__ = inspect.signature(func)
        return guarded


class AgentTurnResetter(FrameProcessor):
    """Returns to ControllerAgent after a terminal worker assistant response."""

    def __init__(self, runtime: AgentRuntime):
        super().__init__()
        self._runtime = runtime
        self._response_started_tool_calls = False

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)
        if self._should_return_to_controller(frame):
            self._runtime.return_to_controller_after_worker_turn()

    def _should_return_to_controller(self, frame) -> bool:
        if isinstance(frame, LLMFullResponseStartFrame):
            self._response_started_tool_calls = False
            return False
        if isinstance(frame, FunctionCallsStartedFrame):
            self._response_started_tool_calls = True
            names = ",".join(call.function_name for call in frame.function_calls)
            logger.debug(f"[AGENT] worker response started tool calls: {names}")
            return False
        if isinstance(frame, LLMFullResponseEndFrame):
            terminal_response = not self._response_started_tool_calls
            self._response_started_tool_calls = False
            if not terminal_response:
                logger.debug("[AGENT] worker response ended after tool call; staying on worker")
            return terminal_response
        return False


def build_default_registry(
    default_model: str,
    extra_models: list[str] | None = None,
    prompt_suffix: str = "",
    agent_models: dict[str, str] | None = None,
) -> AgentRegistry:
    base = Path(__file__).parent
    all_models = [
        "gpt-4o-mini",
        "gpt-4o",
        "gpt-4.1-mini",
        "gpt-4.1",
        "claude-haiku-4-5-20251001",
        "claude-sonnet-4-6",
        "claude-opus-4-8",
        "qwen2.5-coder:7b",
    ]
    # Extra ids a caller wants selectable beyond the built-in list, so per-agent
    # model restrictions do not reject a model the UI offers.
    for model in extra_models or []:
        if model not in all_models:
            all_models.append(model)
    return AgentRegistry(
        [
            AgentSpec(
                agent_id="controller",
                prompt_path=base / "controller" / "prompt.md",
                tool_names=[
                    "list_agents",
                    "list_agent_tools",
                    "read_agent_prompt",
                    "activate_agent",
                    "prompt_reload_with_context",
                    "prompt_reload_reset_context",
                    # Read-only terminal access. The controller has to be able to
                    # see the terminal to route sensibly and to answer "what is
                    # it doing?" — but not to type into it, which stays with
                    # shell.
                    "capture_output",
                    "terminal_since_last_look",
                    "list_terminal_panes",
                ],
                default_model=default_model,
                allowed_models=all_models,
                context_policy="preserve",
            ),
            AgentSpec(
                agent_id="shell",
                prompt_path=base / "shell" / "prompt.md",
                tool_names=[
                    "run_command",
                    "send_input",
                    "send_keys",
                    "capture_output",
                    "terminal_since_last_look",
                    "wait_for_output_idle",
                    "watch_terminal",
                    "start_terminal_monitor",
                    "stop_terminal_monitor",
                    "find_directory",
                    "list_terminal_panes",
                ],
                default_model=default_model,
                allowed_models=all_models,
                context_policy="preserve",
            ),
            AgentSpec(
                agent_id="doc",
                prompt_path=base / "doc" / "prompt.md",
                tool_names=[
                    "list_doc_projects",
                    "enter_doc_mode",
                    "exit_doc_mode",
                    "read_doc",
                    "write_to_doc",
                    "edit_doc",
                    "move_section",
                    "merge_sections",
                ],
                default_model=default_model,
                allowed_models=all_models,
                context_policy="preserve",
            ),
            AgentSpec(
                agent_id="diagram",
                prompt_path=base / "diagram" / "prompt.md",
                tool_names=[
                    "read_doc",
                    "insert_diagram",
                    "update_diagram",
                    "move_diagram",
                    "enter_diagram_focus",
                    "exit_diagram_focus",
                    "revert_diagram_edit",
                    "search_images",
                    "select_image",
                    "resize_image",
                    "cancel_image_search",
                    "done_image",
                    # Sketching: the drawing carries shape, speech carries
                    # meaning, and the model that reads them is chosen
                    # separately from the one running the conversation.
                    "sketch_to_diagram",
                    "set_vision_model",
                    "list_vision_models",
                ],
                default_model=default_model,
                allowed_models=all_models,
                context_policy="preserve",
            ),
            AgentSpec(
                agent_id="image",
                prompt_path=base / "image" / "prompt.md",
                tool_names=[
                    "search_images",
                    "select_image",
                    "resize_image",
                    "cancel_image_search",
                    "done_image",
                ],
                default_model=default_model,
                allowed_models=all_models,
                context_policy="preserve",
            ),
            AgentSpec(
                agent_id="web",
                prompt_path=base / "web" / "prompt.md",
                tool_names=["web_search", "fetch_url"],
                default_model=default_model,
                allowed_models=all_models,
                context_policy="preserve",
            ),
        ],
        prompt_suffix=prompt_suffix,
        agent_models=agent_models,
    )


def _load_prompt_file(path: Path) -> tuple[dict[str, Any], str]:
    raw = path.read_text()
    if not raw.startswith("---\n"):
        return {}, raw
    end = raw.find("\n---\n", 4)
    if end == -1:
        raise ValueError(f"Prompt frontmatter in {path} is missing closing ---")
    frontmatter_text = raw[4:end]
    body = raw[end + len("\n---\n") :]
    frontmatter = yaml.safe_load(frontmatter_text) or {}
    if not isinstance(frontmatter, dict):
        raise ValueError(f"Prompt frontmatter in {path} must be a mapping")
    return frontmatter, body


def _admin_authorized(params: FunctionCallParams) -> bool:
    for message in reversed(getattr(params.context, "messages", []) or []):
        if message.get("role") != "user":
            continue
        content = message.get("content", "")
        if isinstance(content, str):
            normalized = content.strip().lower()
            if _is_synthetic_user_note(normalized):
                continue
            return normalized == "admin" or normalized.startswith(("admin ", "admin:", "admin,"))
    return False


def _strip_tool_call_messages(messages: list[Any]) -> list[Any]:
    cleaned = []
    removed = 0
    for message in messages:
        if _is_tool_call_message(message):
            removed += 1
            continue
        # Dicts are copied so the caller can rewrite the system message without
        # mutating the live context. Anything else — a provider-specific
        # wrapper — is passed through untouched, because dict() would raise on
        # it and dropping it would delete a real turn on every agent handoff.
        entry = message_dict(message)
        cleaned.append(dict(message) if entry is message else message)
    if removed:
        logger.debug(f"[AGENT SWITCH] stripped stale tool-call messages count={removed}")
    return cleaned


def _is_tool_call_message(message: Any) -> bool:
    entry = message_dict(message)
    if entry is None:
        # Not dict-like, so nothing here can identify it as tool plumbing.
        # Kept rather than dropped — discarding a message we cannot read would
        # silently truncate the conversation on every agent handoff.
        return False
    if entry.get("role") == "tool":
        return True
    return any(key in entry for key in ("tool_calls", "tool_call_id", "function_call"))


def _is_synthetic_user_note(content: str) -> bool:
    return content.startswith(("[agent handoff]", "[system note]"))


def _request_matches_activation_hints(user_request: str, hints: list[str]) -> bool:
    request = user_request.strip().lower()
    if not request:
        return True
    request_words = set(re.findall(r"[a-z0-9_./:-]+", request))
    for hint in hints:
        normalized = hint.strip().lower()
        if not normalized:
            continue
        if normalized in request:
            return True
        hint_words = set(re.findall(r"[a-z0-9_./:-]+", normalized))
        if hint_words and hint_words.issubset(request_words):
            return True
    return False


def _truncate(value: Any, limit: int = 240) -> str:
    text = str(value).replace("\n", "\\n")
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _format_args(arguments: dict[str, Any], limit: int = 480) -> str:
    if not arguments:
        return "{}"
    parts = []
    for key in sorted(arguments):
        value = arguments[key]
        if isinstance(value, str):
            rendered = repr(_truncate(value, 160))
        else:
            rendered = _truncate(repr(value), 160)
        parts.append(f"{key}={rendered}")
    return _truncate(", ".join(parts), limit=limit)
