"""Declarative, operator-owned trusted task templates."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Mapping, Optional

import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from .config import PROFILE_NAME_RE, ProfileConfig

TASK_TEMPLATE_FIELDS = frozenset(
    {
        "description",
        "profile",
        "parameters",
        "command",
        "approval_required",
        "plan_ttl_seconds",
        "outputs",
    }
)
TASK_COMMAND_FIELDS = frozenset({"argv", "cwd"})
TASK_OUTPUT_FIELDS = frozenset(
    {"name", "path", "format", "expose", "max_bytes", "required", "schema"}
)
TASK_PARAMETER_RE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")
TASK_ROOTS = ("/tmp", "/workspace")
MAX_TASK_OUTPUT_BYTES = 8 * 1024 * 1024


def _positive_int(name: str, value: Any) -> int:
    if isinstance(value, bool):
        raise RuntimeError(f"{name} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise RuntimeError(f"{name} must be an integer") from error
    if parsed < 1:
        raise RuntimeError(f"{name} must be positive")
    return parsed


def _boolean(name: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise RuntimeError(f"{name} must be a boolean")
    return value


def _safe_path(name: str, value: Any) -> str:
    path = str(value or "")
    candidate = PurePosixPath(path)
    if not path.startswith("/") or ".." in candidate.parts:
        raise RuntimeError(f"{name} must be an absolute safe path")
    normalized = str(candidate)
    if not any(
        normalized == root or normalized.startswith(root + "/")
        for root in TASK_ROOTS
    ):
        raise RuntimeError(f"{name} must remain under /workspace or /tmp")
    return normalized


def _check_schema(name: str, value: Mapping[str, Any]) -> Dict[str, Any]:
    schema = dict(value)

    def visit(item: Any) -> None:
        if isinstance(item, Mapping):
            for keyword in ("$ref", "$dynamicRef", "$recursiveRef"):
                reference = item.get(keyword)
                if isinstance(reference, str) and not reference.startswith("#"):
                    raise RuntimeError(f"{name} must not contain external schema references")
            for child in item.values():
                visit(child)
        elif isinstance(item, list):
            for child in item:
                visit(child)

    visit(schema)
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as error:
        raise RuntimeError(f"{name} is invalid") from error
    return schema


@dataclass(frozen=True)
class TaskOutputConfig:
    """One operator-approved task output and its disclosure policy."""

    name: str
    path: str
    format: str = "json"
    expose: str = "content"
    max_bytes: int = 64 * 1024
    required: bool = True
    schema: Optional[Dict[str, Any]] = None

    @classmethod
    def from_mapping(cls, value: Any, *, index: int) -> "TaskOutputConfig":
        if not isinstance(value, Mapping):
            raise RuntimeError(f"task output {index} must be an object")
        unknown = set(value) - TASK_OUTPUT_FIELDS
        if unknown:
            raise RuntimeError(
                f"task output {index} contains unsupported fields: "
                + ", ".join(sorted(str(item) for item in unknown))
            )
        name = str(value.get("name", ""))
        if not PROFILE_NAME_RE.fullmatch(name):
            raise RuntimeError(f"task output {index} has an invalid name")
        path = _safe_path(f"task output {name!r} path", value.get("path"))
        output_format = str(value.get("format", "json"))
        if output_format not in {"json", "text"}:
            raise RuntimeError(f"task output {name!r} format must be json or text")
        expose = str(value.get("expose", "content"))
        if expose not in {"content", "digest"}:
            raise RuntimeError(f"task output {name!r} expose must be content or digest")
        schema_value = value.get("schema")
        if schema_value is not None:
            if output_format != "json" or not isinstance(schema_value, Mapping):
                raise RuntimeError(f"task output {name!r} schema requires JSON output")
            schema_value = _check_schema(f"task output {name!r} schema", schema_value)
        max_bytes = _positive_int(
            f"task output {name}.max_bytes", value.get("max_bytes", 64 * 1024)
        )
        if max_bytes > MAX_TASK_OUTPUT_BYTES:
            raise RuntimeError(
                f"task output {name!r} max_bytes must not exceed {MAX_TASK_OUTPUT_BYTES}"
            )
        return cls(
            name=name,
            path=path,
            format=output_format,
            expose=expose,
            max_bytes=max_bytes,
            required=_boolean(
                f"task output {name}.required", value.get("required", True)
            ),
            schema=dict(schema_value) if schema_value is not None else None,
        )


@dataclass(frozen=True)
class TaskTemplateConfig:
    """A fixed command, parameter contract, policy profile, and output contract."""

    name: str
    description: str
    profile: str
    parameters: Dict[str, Any]
    argv: tuple[str, ...]
    cwd: str = "/workspace"
    approval_required: bool = True
    plan_ttl_seconds: int = 900
    outputs: tuple[TaskOutputConfig, ...] = ()

    @classmethod
    def from_mapping(
        cls,
        name: str,
        value: Any,
        *,
        profiles: Mapping[str, ProfileConfig],
        defaults: Optional[Mapping[str, Any]] = None,
    ) -> "TaskTemplateConfig":
        if not PROFILE_NAME_RE.fullmatch(name):
            raise RuntimeError(f"invalid task template name: {name!r}")
        if not isinstance(value, Mapping):
            raise RuntimeError(f"task template {name!r} must be an object")
        merged: Dict[str, Any] = dict(defaults or {})
        merged.update(dict(value))
        unknown = set(merged) - TASK_TEMPLATE_FIELDS
        if unknown:
            raise RuntimeError(
                f"task template {name!r} contains unsupported fields: "
                + ", ".join(sorted(str(item) for item in unknown))
            )
        profile = str(merged.get("profile", ""))
        if profile not in profiles:
            raise RuntimeError(f"task template {name!r} references an unknown profile")
        description = str(merged.get("description", "")).strip()
        if not description or len(description.encode("utf-8")) > 1024:
            raise RuntimeError(f"task template {name!r} requires a short description")

        parameters_value = merged.get("parameters")
        if not isinstance(parameters_value, Mapping):
            raise RuntimeError(f"task template {name!r} parameters must be a JSON Schema")
        parameters = dict(parameters_value)
        if (
            parameters.get("type") != "object"
            or parameters.get("additionalProperties") is not False
        ):
            raise RuntimeError(
                f"task template {name!r} parameter schema must be a closed object"
            )
        parameters = _check_schema(
            f"task template {name!r} parameter schema", parameters
        )

        command = merged.get("command")
        if not isinstance(command, Mapping):
            raise RuntimeError(f"task template {name!r} command must be an object")
        command_unknown = set(command) - TASK_COMMAND_FIELDS
        if command_unknown:
            raise RuntimeError(
                f"task template {name!r} command contains unsupported fields: "
                + ", ".join(sorted(str(item) for item in command_unknown))
            )
        argv_value = command.get("argv")
        if not isinstance(argv_value, list) or not argv_value:
            raise RuntimeError(
                f"task template {name!r} command.argv must be a non-empty list"
            )
        if not all(isinstance(item, str) and item for item in argv_value):
            raise RuntimeError(f"task template {name!r} command.argv must contain strings")
        properties = parameters.get("properties", {})
        if not isinstance(properties, Mapping):
            raise RuntimeError(f"task template {name!r} parameter properties must be an object")
        for item in argv_value:
            if "${" not in item:
                continue
            match = TASK_PARAMETER_RE.fullmatch(item)
            if not match or match.group(1) not in properties:
                raise RuntimeError(
                    f"task template {name!r} command arguments must use whole, "
                    "declared placeholders"
                )
        cwd = _safe_path(
            f"task template {name!r} command.cwd", command.get("cwd", "/workspace")
        )

        output_values = merged.get("outputs", [])
        if not isinstance(output_values, list):
            raise RuntimeError(f"task template {name!r} outputs must be a list")
        outputs = tuple(
            TaskOutputConfig.from_mapping(item, index=index)
            for index, item in enumerate(output_values)
        )
        output_names = [item.name for item in outputs]
        if len(output_names) != len(set(output_names)):
            raise RuntimeError(f"task template {name!r} output names must be unique")
        return cls(
            name=name,
            description=description,
            profile=profile,
            parameters=parameters,
            argv=tuple(argv_value),
            cwd=cwd,
            approval_required=_boolean(
                f"task template {name}.approval_required",
                merged.get("approval_required", True),
            ),
            plan_ttl_seconds=_positive_int(
                f"task template {name}.plan_ttl_seconds",
                merged.get("plan_ttl_seconds", 900),
            ),
            outputs=outputs,
        )

    @property
    def digest(self) -> str:
        value = {
            "name": self.name,
            "profile": self.profile,
            "parameters": self.parameters,
            "argv": self.argv,
            "cwd": self.cwd,
            "approval_required": self.approval_required,
            "plan_ttl_seconds": self.plan_ttl_seconds,
            "outputs": [vars(item) for item in self.outputs],
        }
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def validate_parameters(self, value: Any) -> Dict[str, Any]:
        try:
            Draft202012Validator(self.parameters).validate(value)
        except ValidationError as error:
            location = ".".join(str(item) for item in error.absolute_path) or "$"
            raise ValueError(f"parameters do not match schema at {location}") from error
        assert isinstance(value, dict)
        return dict(value)

    def validate_output(self, output: TaskOutputConfig, value: Any) -> None:
        if output.schema is None:
            return
        try:
            Draft202012Validator(output.schema).validate(value)
        except ValidationError as error:
            raise ValueError(f"output {output.name!r} does not match its schema") from error

    def render_command(self, parameters: Mapping[str, Any]) -> str:
        rendered = []
        for item in self.argv:
            match = TASK_PARAMETER_RE.fullmatch(item)
            if match:
                value = parameters[match.group(1)]
                if isinstance(value, bool):
                    rendered.append("true" if value else "false")
                elif value is None or isinstance(value, (dict, list)):
                    raise ValueError("command placeholders support scalar parameters only")
                else:
                    rendered.append(str(value))
            else:
                rendered.append(item)
        return shlex.join(rendered)

    def public_contract(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "profile": self.profile,
            "parameters": self.parameters,
            "approval_required": self.approval_required,
            "plan_ttl_seconds": self.plan_ttl_seconds,
            "outputs": [
                {
                    "name": item.name,
                    "format": item.format,
                    "expose": item.expose,
                    "required": item.required,
                }
                for item in self.outputs
            ],
            "template_sha256": self.digest,
        }


def load_task_templates(
    path: Optional[str], *, profiles: Mapping[str, ProfileConfig]
) -> Dict[str, TaskTemplateConfig]:
    if not path:
        return {}
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except OSError as error:
        raise RuntimeError(f"cannot read task template file {path!r}") from error
    except yaml.YAMLError as error:
        raise RuntimeError(f"task template file {path!r} is not valid YAML") from error
    if not isinstance(raw, Mapping):
        raise RuntimeError("task template file must contain an object")
    unknown = set(raw) - {"defaults", "tasks"}
    if unknown:
        raise RuntimeError(
            "task template file contains unsupported fields: "
            + ", ".join(sorted(str(item) for item in unknown))
        )
    defaults = raw.get("defaults", {})
    values = raw.get("tasks")
    if not isinstance(defaults, Mapping):
        raise RuntimeError("task template defaults must be an object")
    if not isinstance(values, Mapping) or not values:
        raise RuntimeError("task template file must define at least one task")
    return {
        str(name): TaskTemplateConfig.from_mapping(
            str(name), value, profiles=profiles, defaults=defaults
        )
        for name, value in values.items()
    }
