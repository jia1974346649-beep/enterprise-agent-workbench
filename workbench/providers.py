"""Explicit planners: a repeatable offline example and optional local Ollama.

The offline planner is rules-based, not an LLM. Ollama is opt-in, has no API key,
and is restricted to loopback HTTP with redirects and environment proxies disabled.
Neither provider executes tools; the domain owns tool validation and authorization.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request


class ProviderError(Exception):
    """A safe, public error code; never contains raw network/model data."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class OfflineProvider:
    mode = "offline"

    def next(self, messages: list[dict], tools: list[dict], scenario=None) -> dict:
        """Choose a fixed demonstration tool, then describe its actual output."""
        if messages[-1]["role"] == "tool":
            result = json.loads(messages[-1]["content"])
            name = messages[-1].get("tool_name", "")
            if name == "search_knowledge":
                matches = result["matches"]
                content = "找到以下合成知识资料：" + "；".join(
                    f"{row['title']}：{row['excerpt']}" for row in matches
                ) if matches else "没有检索到匹配资料。可以试试：报销、上线、权限。"
            elif name == "list_tasks":
                content = "当前租户任务：" + "；".join(
                    f"{row['title']}（{row['status']}）" for row in result["tasks"]
                ) if result["tasks"] else "当前筛选下没有任务。"
            else:
                content = "工具已返回结果。"
            return {"content": content, "tool_calls": []}
        text = messages[-1]["content"].strip()
        lowered = text.lower()
        if scenario == "forbidden" or any(word in lowered for word in (
            "导出所有租户", "删除数据库", "执行命令", "绕过权限", "ignore previous",
            "system prompt", "run shell", "drop table", "reveal secret",
        )):
            name, arguments = "admin_export_data", {}
        elif scenario == "cross_tenant" or any(word in lowered for word in (
            "另一个租户", "跨租户", "beta租户", "beta 租户", "tenant beta",
        )):
            name, arguments = "list_tasks", {"tenant_id": "beta"}
        elif scenario == "create_task" or any(word in lowered for word in (
            "创建", "新增", "添加任务", "create task", "add task",
        )):
            title = re.sub(r"^(?:请)?(?:帮我)?(?:创建|新增|添加)(?:一个|一条)?(?:任务)?[：:，,\s]*", "", text)
            title = re.sub(r"^(?:please\s+)?(?:create|add)\s+(?:a\s+)?task[\s:]*", "", title, flags=re.I)
            name, arguments = "create_task", {"title": (title or "整理演示项目说明")[:120]}
        elif scenario == "tasks" or any(word in lowered for word in ("任务", "待办", "task", "todo")):
            name, arguments = "list_tasks", {}
        else:
            name, arguments = "search_knowledge", {"query": text[:200], "limit": 3}
        return {"content": "离线规则选择了一个演示工具。", "tool_calls": [
            {"id": "offline-call", "name": name, "arguments": arguments}
        ]}


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class OllamaProvider:
    """Ollama /api/chat adapter. Live model quality is not implied by protocol tests."""

    mode = "ollama"
    MAX_RESPONSE_BYTES = 1_048_576

    def __init__(self, model: str, base_url="http://127.0.0.1:11434", timeout=15):
        if not isinstance(base_url, str):
            raise ProviderError("invalid_local_model_url")
        try:
            parsed = urllib.parse.urlsplit(base_url)
            port = parsed.port
        except (TypeError, ValueError):
            raise ProviderError("invalid_local_model_url") from None
        if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
                or port == 0):
            raise ProviderError("invalid_local_model_url")
        if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,119}", model):
            raise ProviderError("invalid_local_model_name")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 30:
            raise ProviderError("invalid_local_model_timeout")
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirects())

    def next(self, messages: list[dict], tools: list[dict], scenario=None) -> dict:
        # Native Ollama expects tool_name on tool responses. The domain constructs
        # every message; scenario selection only controls the offline planner.
        payload = json.dumps({"model": self.model, "messages": messages, "tools": tools,
                              "stream": False, "options": {"temperature": 0}}, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(self.base_url + "/api/chat", data=payload,
                                         headers={"Content-Type": "application/json"}, method="POST")
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                raw = response.read(self.MAX_RESPONSE_BYTES + 1)
            if len(raw) > self.MAX_RESPONSE_BYTES:
                raise ProviderError("model_response_too_large")
            body = json.loads(raw.decode("utf-8"))
        except ProviderError:
            raise
        except urllib.error.HTTPError as error:
            # HTTPError also owns the failed response/socket. In particular,
            # rejected redirects never enter the response context manager.
            try:
                error.close()
            finally:
                raise ProviderError("local_model_unavailable") from None
        except (OSError, ValueError, urllib.error.URLError):
            raise ProviderError("local_model_unavailable") from None
        if not isinstance(body, dict) or not isinstance(body.get("message"), dict):
            raise ProviderError("invalid_model_response")
        message = body["message"]
        content = message.get("content", "")
        calls = message.get("tool_calls", [])
        if not isinstance(content, str) or len(content) > 8000 or not isinstance(calls, list) or len(calls) > 4:
            raise ProviderError("invalid_model_response")
        normalized = []
        for index, call in enumerate(calls):
            if not isinstance(call, dict) or not isinstance(call.get("function"), dict):
                raise ProviderError("invalid_model_response")
            function = call["function"]
            if not isinstance(function.get("name"), str) or not isinstance(function.get("arguments"), dict):
                raise ProviderError("invalid_model_response")
            if len(function["name"]) > 80:
                raise ProviderError("invalid_model_response")
            normalized.append({"id": f"model-call-{index}", "name": function["name"],
                               "arguments": function["arguments"]})
        if not content.strip() and not normalized:
            raise ProviderError("empty_model_response")
        return {"content": content, "tool_calls": normalized}


def provider_from_environment(environ=None):
    """No local model request occurs unless WORKBENCH_MODE=ollama is set."""
    config = os.environ if environ is None else environ
    mode = config.get("WORKBENCH_MODE", "offline")
    if mode == "offline":
        return OfflineProvider()
    if mode != "ollama":
        raise ProviderError("invalid_workbench_mode")
    return OllamaProvider(model=config.get("WORKBENCH_OLLAMA_MODEL", ""),
                          base_url=config.get("WORKBENCH_OLLAMA_URL", "http://127.0.0.1:11434"))
