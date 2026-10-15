"""Versioned internal template presets and late-bound Secret injection."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

import yaml

from .backend_config import NAME_RE, resolve_references


def validate_openclaw_models(config: Mapping[str, Any]) -> list[str]:
    models, agents = config.get("models", {}), config.get("agents", {})
    if not isinstance(models, Mapping) or not isinstance(agents, Mapping):
        raise RuntimeError("OpenClaw models and agents must be objects")
    providers = models.get("providers", {})
    if not isinstance(providers, Mapping):
        raise RuntimeError("OpenClaw providers must be an object")
    known: set[str] = set()
    for name, provider in providers.items():
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}", name):
            raise RuntimeError("OpenClaw provider names must not contain whitespace or slashes")
        if not isinstance(provider, Mapping) or not isinstance(provider.get("models", []), list):
            raise RuntimeError("invalid OpenClaw provider model registry")
        for model in provider.get("models", []):
            if (
                not isinstance(model, Mapping)
                or not isinstance(model.get("id"), str)
                or not model["id"]
                or any(char.isspace() for char in model["id"])
            ):
                raise RuntimeError("each OpenClaw provider model requires an id")
            identifier = name + "/" + model["id"]
            if identifier in known:
                raise RuntimeError("duplicate OpenClaw model identifier")
            known.add(identifier)
    defaults = agents.get("defaults", {})
    if not isinstance(defaults, Mapping):
        raise RuntimeError("OpenClaw defaults must be an object")
    selection = defaults.get("model", {})
    if not isinstance(selection, (str, Mapping)) or not isinstance(
        defaults.get("models", {}), Mapping
    ):
        raise RuntimeError("invalid OpenClaw model selection")
    if isinstance(selection, Mapping) and not isinstance(selection.get("fallbacks", []), list):
        raise RuntimeError("OpenClaw fallbacks must be a list")
    references = (
        [selection]
        if isinstance(selection, str)
        else [selection.get("primary"), *selection.get("fallbacks", [])]
    )
    references.extend(defaults.get("models", {}))
    if any(reference and reference not in known for reference in references):
        raise RuntimeError("OpenClaw default/alias model is missing from the provider registry")
    return sorted(known)


@dataclass(frozen=True)
class TemplatePreset:
    name: str
    version: str
    backend_id: str
    template: str
    env_vars: Mapping[str, Any] = field(default_factory=dict, repr=False)
    files: Mapping[str, Any] = field(default_factory=dict, repr=False)
    bootstrap_argv: tuple[str, ...] = ()
    preflight_argv: tuple[str, ...] = ()
    models: tuple[str, ...] = ()

    def materialize(self) -> tuple[dict[str, str], dict[str, str]]:
        # Resolve Secrets at creation rather than storing values in a template
        # image, lease state, model-facing response, or build request.
        env = resolve_references(dict(self.env_vars))
        if not all(isinstance(value, str) for value in env.values()):
            raise RuntimeError("template environment values must resolve to strings")
        files: dict[str, str] = {}
        for path, raw in self.files.items():
            content = resolve_references(raw)
            files[path] = (
                content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            )
            if len(files[path].encode()) > 1024 * 1024:
                raise RuntimeError("template configuration file exceeds the size limit")
            if PurePosixPath(path).name == "openclaw.json":
                try:
                    config = json.loads(files[path])
                except ValueError as error:
                    raise RuntimeError("OpenClaw configuration must be JSON") from error
                if not isinstance(config, Mapping):
                    raise RuntimeError("OpenClaw configuration must be an object")
                validate_openclaw_models(config)
        return env, files


def load_template_presets(path: str | None) -> dict[str, TemplatePreset]:
    if not path:
        return {}
    try:
        document = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise RuntimeError("internal template catalog cannot be loaded") from error
    if (
        not isinstance(document, Mapping)
        or set(document) != {"templates"}
        or not isinstance(document["templates"], Mapping)
    ):
        raise RuntimeError("internal template catalog must contain templates")
    presets = {}
    fields = {
        "version",
        "backend_id",
        "template",
        "env_vars",
        "files",
        "bootstrap_argv",
        "preflight_argv",
    }
    for name, raw in document["templates"].items():
        if (
            not isinstance(name, str)
            or not NAME_RE.fullmatch(name)
            or not isinstance(raw, Mapping)
            or set(raw) - fields
        ):
            raise RuntimeError("invalid internal template preset")
        if not all(isinstance(raw.get(key), str) and raw[key] for key in ("version", "template")):
            raise RuntimeError("internal templates require version and template")
        env = raw.get("env_vars", {})
        files = raw.get("files", {})
        if not isinstance(env, Mapping) or not isinstance(files, Mapping):
            raise RuntimeError("internal template env_vars/files must be objects")
        for key in env:
            if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                raise RuntimeError("invalid template environment name")
        models: list[str] = []
        for destination, content in files.items():
            if not isinstance(destination, str):
                raise RuntimeError("template file paths must be strings")
            p = PurePosixPath(destination)
            if (
                not p.is_absolute()
                or ".." in p.parts
                or not any(
                    str(p).startswith(prefix)
                    for prefix in (
                        "/root/.openclaw/",
                        "/home/",
                        "/workspace/",
                        "/tmp/",
                        "/etc/mcp-gateway/",
                    )
                )
            ):
                raise RuntimeError("template file path is outside configuration roots")
            if p.name == "openclaw.json" and isinstance(content, Mapping):
                models.extend(validate_openclaw_models(content))
        commands = {}
        for key in ("bootstrap_argv", "preflight_argv"):
            argv = raw.get(key, [])
            if not isinstance(argv, list) or not all(
                isinstance(item, str) and item for item in argv
            ):
                raise RuntimeError("template commands must be fixed argument lists")
            commands[key] = tuple(argv)
        presets[name] = TemplatePreset(
            name,
            raw["version"],
            raw.get("backend_id", "default"),
            raw["template"],
            dict(env),
            dict(files),
            commands["bootstrap_argv"],
            commands["preflight_argv"],
            tuple(models),
        )
    return presets
