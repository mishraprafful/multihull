from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import httpx
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

SPECS = Path(__file__).resolve().parent / "specs"
METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace", "query")
JSON_MEDIA = re.compile(r"^application/(?:[\w.+-]+\+)?json$")
INTEGER = re.compile(r"^-?\d+$")
NUMBER = re.compile(r"^-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?$")
SHOWN_LIMITS = frozenset(
    {
        "const",
        "enum",
        "exclusiveMaximum",
        "exclusiveMinimum",
        "maxItems",
        "maxLength",
        "maxProperties",
        "maximum",
        "minItems",
        "minLength",
        "minProperties",
        "minimum",
        "multipleOf",
        "pattern",
        "type",
    }
)
EXTRA_KEYWORDS = ("additionalProperties", "unevaluatedProperties")
ROOT = "(root)"
UNDOCUMENTED = "undocumented"
ERROR_STATUS = 400
MAX_REPORTED = 5
VIOLATIONS: list[str] = []


class SpecViolation(AssertionError):
    pass


@dataclass(frozen=True)
class Problem:
    where: str
    field: str
    rule: str
    detail: str

    def __str__(self) -> str:
        return f"{self.where} {self.field}: {self.detail}"


@dataclass(frozen=True)
class Allowance:
    method: str
    template: str
    where: str
    field: str
    rule: str

    def covers(self, method: str, template: str, problem: Problem) -> bool:
        return (self.method, self.template, self.where, self.field, self.rule) == (
            method,
            template,
            problem.where,
            problem.field,
            problem.rule,
        )


ALLOWANCES: dict[str, tuple[Allowance, ...]] = {
    "replicate": (Allowance("get", "/deployments", "query parameter", "cursor", UNDOCUMENTED),),
}


@dataclass(frozen=True)
class Operation:
    method: str
    template: str
    pointer: str
    pattern: re.Pattern[str]
    names: tuple[str, ...]
    operation_id: str | None

    @property
    def label(self) -> str:
        shown = f"{self.method.upper()} {self.template}"
        return f"{shown} ({self.operation_id})" if self.operation_id else shown


def escape(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def unescape(token: str) -> str:
    return token.replace("~1", "/").replace("~0", "~")


def allow_null(node: Any) -> Any:
    if isinstance(node, list):
        return [allow_null(item) for item in node]
    if not isinstance(node, dict):
        return node
    converted = {key: allow_null(value) for key, value in node.items()}
    if converted.pop("nullable", False) is True:
        if isinstance(converted.get("type"), str):
            converted["type"] = [converted["type"], "null"]
        if isinstance(converted.get("enum"), list) and None not in converted["enum"]:
            converted["enum"] = [*converted["enum"], None]
    return converted


def compile_template(template: str) -> tuple[re.Pattern[str], tuple[str, ...]]:
    names: list[str] = []
    parts: list[str] = []
    for literal, name in re.findall(r"([^{]*)(?:\{([^}]+)\})?", template):
        parts.append(re.escape(literal))
        if name:
            names.append(name)
            parts.append("([^/]+)")
    return re.compile("^" + "".join(parts) + "$"), tuple(names)


def literal_segments(templates: Iterable[str]) -> frozenset[str]:
    return frozenset(
        segment
        for template in templates
        for segment in template.split("/")
        if segment and not segment.startswith("{")
    )


def media_type(headers: httpx.Headers) -> str:
    return headers.get("content-type", "").split(";")[0].strip().lower()


def coerced(raw: str) -> list[Any]:
    found: list[Any] = []
    if INTEGER.match(raw):
        found.append(int(raw))
    if NUMBER.match(raw):
        found.append(float(raw))
    if raw in ("true", "false"):
        found.append(raw == "true")
    return [*found, raw]


def extra_fields(error: ValidationError) -> list[str]:
    instance = error.instance if isinstance(error.instance, dict) else {}
    known = set(error.schema.get("properties") or {})
    patterns = [re.compile(p) for p in error.schema.get("patternProperties") or {}]
    return sorted(
        str(name)
        for name in instance
        if name not in known and not any(p.search(str(name)) for p in patterns)
    )


def detail(error: ValidationError) -> str:
    keyword = str(error.validator)
    if keyword == "required":
        instance = error.instance if isinstance(error.instance, dict) else {}
        missing = [str(name) for name in error.validator_value if name not in instance]
        return f"missing required field {', '.join(missing)}"
    if keyword in SHOWN_LIMITS:
        return f"breaks {keyword} {json.dumps(error.validator_value)}"
    return f"breaks {keyword}"


def joined(path: Iterable[Any]) -> str:
    return "/".join(str(part) for part in path) or ROOT


def field_of(error: ValidationError) -> str:
    return joined(error.absolute_path)


def undocumented_field(where: str, path: Iterable[Any]) -> Problem:
    return Problem(where, joined(path), UNDOCUMENTED, "undocumented field")


def problems_of(where: str, error: ValidationError) -> list[Problem]:
    if error.validator == "unevaluatedProperties":
        return []
    if error.validator == "additionalProperties":
        return [
            undocumented_field(where, [*error.absolute_path, name]) for name in extra_fields(error)
        ]
    return [Problem(where, field_of(error), str(error.validator), detail(error))]


def leaves(error: ValidationError) -> list[ValidationError]:
    if error.validator not in ("anyOf", "oneOf") or not error.context:
        return [error]
    branches: dict[Any, list[ValidationError]] = {}
    for sub in error.context:
        branches.setdefault(sub.relative_schema_path[0], []).append(sub)
    fitting = [
        found
        for found in branches.values()
        if not any(e.validator == "type" and not e.relative_path for e in found)
    ]
    if len(fitting) != 1:
        return [error]
    return [leaf for sub in fitting[0] for leaf in leaves(sub)]


def opens(schema: dict[str, Any]) -> bool:
    return "patternProperties" in schema or any(
        schema.get(keyword, False) is not False for keyword in EXTRA_KEYWORDS
    )


def closed(schemas: list[dict[str, Any]]) -> bool:
    shut = any(
        "properties" in schema or any(schema.get(keyword) is False for keyword in EXTRA_KEYWORDS)
        for schema in schemas
    )
    return shut and not any(opens(schema) for schema in schemas)


def query_values(request: httpx.Request) -> dict[str, list[str]]:
    values: dict[str, list[str]] = {}
    for name, value in request.url.params.multi_items():
        values.setdefault(name, []).append(value)
    return values


class Contract:
    def __init__(self, provider: str) -> None:
        spec = allow_null(json.loads((SPECS / f"{provider}.openapi.json").read_text()))
        server = urlsplit(spec["servers"][0]["url"])
        self.title = str(spec["info"]["title"])
        self.host = server.hostname
        self.base_path = server.path.rstrip("/")
        self.spec = spec
        self.uri = f"urn:multihull:openapi:{provider}"
        resource = Resource.from_contents(spec, default_specification=DRAFT202012)
        self.registry: Registry[Any] = Registry().with_resource(self.uri, resource)
        self.operations = sorted(self.collect(), key=lambda o: (len(o.names), o.template))
        self.literals = literal_segments(spec["paths"])
        self.allowances = ALLOWANCES.get(provider, ())
        self.validators: dict[str, Draft202012Validator] = {}

    def collect(self) -> Iterator[Operation]:
        for template, item in self.spec["paths"].items():
            pattern, names = compile_template(template)
            for method in METHODS:
                if method in item:
                    yield Operation(
                        method=method,
                        template=template,
                        pointer=f"/paths/{escape(template)}/{method}",
                        pattern=pattern,
                        names=names,
                        operation_id=item[method].get("operationId"),
                    )

    def exchange(
        self, request: httpx.Request, handle: Callable[[httpx.Request], httpx.Response]
    ) -> httpx.Response:
        if request.url.host != self.host:
            return handle(request)
        operation, path_values = self.match(request)
        self.check(operation, self.request_problems(operation, path_values, request))
        response = handle(request)
        self.check(operation, self.response_problems(operation, response))
        return response

    def check(self, operation: Operation, problems: Iterable[Problem]) -> None:
        kept = [
            str(problem)
            for problem in problems
            if not any(
                a.covers(operation.method, operation.template, problem) for a in self.allowances
            )
        ]
        if kept:
            raise self.violation(operation.label, list(dict.fromkeys(kept)))

    def violation(self, label: str, problems: list[str]) -> SpecViolation:
        shown = problems[:MAX_REPORTED]
        more = len(problems) - len(shown)
        tail = f" (and {more} more)" if more > 0 else ""
        message = f"{self.title} {label}: " + "; ".join(shown) + tail
        VIOLATIONS.append(message)
        return SpecViolation(message)

    def shape(self, path: str) -> str:
        return "/".join(s if not s or s in self.literals else "{?}" for s in path.split("/"))

    def match(self, request: httpx.Request) -> tuple[Operation, dict[str, str]]:
        raw = request.url.raw_path.decode().split("?")[0]
        method = request.method.lower()
        if raw.startswith(f"{self.base_path}/"):
            path = raw.removeprefix(self.base_path)
            for operation in self.operations:
                found = operation.pattern.match(path)
                if found and operation.method == method:
                    return operation, dict(zip(operation.names, found.groups(), strict=True))
        label = f"{request.method} {self.shape(raw)}"
        raise self.violation(label, ["no operation in the spec matches this method and path"])

    def node(self, pointer: str) -> Any:
        found: Any = self.spec
        for token in pointer.lstrip("/").split("/"):
            key = unescape(token)
            found = found[int(key)] if isinstance(found, list) else found[key]
        return found

    def resolved(self, pointer: str) -> tuple[Any, str]:
        found = self.node(pointer)
        while isinstance(found, dict) and isinstance(found.get("$ref"), str):
            pointer = found["$ref"].removeprefix("#")
            found = self.node(pointer)
        return found, pointer

    def validator(self, pointer: str) -> Draft202012Validator:
        cached = self.validators.get(pointer)
        if cached is None:
            schema = {"$ref": f"{self.uri}#{pointer}"}
            cached = Draft202012Validator(schema, registry=self.registry)
            self.validators[pointer] = cached
        return cached

    def schema_problems(self, where: str, pointer: str, instance: Any) -> list[Problem]:
        problems = [
            problem
            for error in sorted(self.validator(pointer).iter_errors(instance), key=field_of)
            for leaf in leaves(error)
            for problem in problems_of(where, leaf)
        ]
        problems.extend(
            undocumented_field(where, path) for path in self.undocumented([pointer], instance, ())
        )
        return problems

    def branches(
        self, pointer: str, instance: Any, seen: frozenset[str] = frozenset()
    ) -> Iterator[tuple[str, dict[str, Any]]]:
        schema = self.node(pointer)
        if not isinstance(schema, dict):
            return
        ref = schema.get("$ref")
        if isinstance(ref, str) and ref not in seen:
            yield from self.branches(ref.removeprefix("#"), instance, seen | {ref})
        yield pointer, schema
        for keyword in ("allOf", "anyOf", "oneOf"):
            options = [f"{pointer}/{keyword}/{i}" for i, _ in enumerate(schema.get(keyword) or [])]
            if keyword != "allOf":
                options = [o for o in options if self.validator(o).is_valid(instance)] or options
            for option in options:
                yield from self.branches(option, instance, seen)

    def undocumented(
        self, pointers: list[str], instance: Any, path: tuple[str, ...]
    ) -> Iterator[tuple[str, ...]]:
        flat = [branch for pointer in pointers for branch in self.branches(pointer, instance)]
        if isinstance(instance, list):
            items = [f"{p}/items" for p, schema in flat if isinstance(schema.get("items"), dict)]
            for index, item in enumerate(instance):
                yield from self.undocumented(items, item, (*path, str(index)))
            return
        if not isinstance(instance, dict):
            return
        shut = closed([schema for _, schema in flat])
        for key, value in instance.items():
            nested = [
                f"{p}/properties/{escape(str(key))}"
                for p, schema in flat
                if key in (schema.get("properties") or {})
            ]
            nested += [
                f"{p}/additionalProperties"
                for p, schema in flat
                if isinstance(schema.get("additionalProperties"), dict)
            ]
            if shut and not nested:
                yield (*path, str(key))
            else:
                yield from self.undocumented(nested, value, (*path, str(key)))

    def parameters(self, operation: Operation) -> dict[tuple[str, str], str]:
        found: dict[tuple[str, str], str] = {}
        item_pointer = operation.pointer.rsplit("/", 1)[0]
        for owner in (item_pointer, operation.pointer):
            for index, _ in enumerate(self.node(owner).get("parameters") or []):
                parameter, pointer = self.resolved(f"{owner}/parameters/{index}")
                found[(parameter["in"], parameter["name"])] = pointer
        return found

    def parameter_problems(
        self, operation: Operation, location: str, values: Mapping[str, list[str]]
    ) -> list[Problem]:
        where = f"{location} parameter"
        declared = {
            name: pointer
            for (place, name), pointer in self.parameters(operation).items()
            if place == location
        }
        problems = [
            Problem(where, name, UNDOCUMENTED, "undocumented")
            for name in values
            if name not in declared
        ]
        for name, pointer in declared.items():
            parameter = self.node(pointer)
            given = values.get(name)
            if not given:
                if parameter.get("required"):
                    problems.append(Problem(where, name, "required", "missing"))
                continue
            if "schema" in parameter:
                for raw in given:
                    problems.extend(self.coerced_problems(f"{where} {name}", pointer, raw))
        return problems

    def coerced_problems(self, where: str, pointer: str, raw: str) -> list[Problem]:
        validator = self.validator(f"{pointer}/schema")
        candidates = coerced(raw)
        if any(validator.is_valid(candidate) for candidate in candidates):
            return []
        return self.schema_problems(where, f"{pointer}/schema", candidates[0])

    def request_problems(
        self, operation: Operation, path_values: Mapping[str, str], request: httpx.Request
    ) -> list[Problem]:
        paths = {name: [unquote(value)] for name, value in path_values.items()}
        return [
            *self.parameter_problems(operation, "path", paths),
            *self.parameter_problems(operation, "query", query_values(request)),
            *self.body_problems(operation, request),
        ]

    def body_problems(self, operation: Operation, request: httpx.Request) -> list[Problem]:
        where = "request body"
        content = request.content
        if "requestBody" not in self.node(operation.pointer):
            return [Problem(where, ROOT, "absent", "the operation takes none")] if content else []
        body, pointer = self.resolved(f"{operation.pointer}/requestBody")
        if not content:
            return [Problem(where, ROOT, "required", "missing")] if body.get("required") else []
        media = media_type(request.headers)
        if media not in (body.get("content") or {}):
            return [Problem(where, ROOT, "content-type", f"content type {media} not accepted")]
        if not JSON_MEDIA.match(media) or "schema" not in body["content"][media]:
            return []
        schema_pointer = f"{pointer}/content/{escape(media)}/schema"
        return self.schema_problems(where, schema_pointer, json.loads(content))

    def response_pointer(self, operation: Operation, status: int) -> str | None:
        responses = self.node(operation.pointer).get("responses") or {}
        fallback = ("default",) if status >= ERROR_STATUS else ()
        for key in (str(status), f"{status // 100}XX", *fallback):
            if key in responses:
                return f"{operation.pointer}/responses/{escape(key)}"
        return None

    def response_problems(self, operation: Operation, response: httpx.Response) -> list[Problem]:
        status = response.status_code
        where = f"response {status}"
        located = self.response_pointer(operation, status)
        if located is None:
            if status >= ERROR_STATUS:
                return []
            return [Problem(where, ROOT, "status", "status not documented")]
        documented, pointer = self.resolved(located)
        content = documented.get("content") or {}
        response.read()
        if not response.content:
            needs_body = any(JSON_MEDIA.match(media) for media in content)
            return [Problem(where, ROOT, "body", "body missing")] if needs_body else []
        if not content:
            return [Problem(where, ROOT, "body", "the spec documents no body")]
        media = media_type(response.headers)
        if media not in content:
            return [Problem(where, ROOT, "content-type", f"content type {media} not documented")]
        if not JSON_MEDIA.match(media) or "schema" not in content[media]:
            return []
        schema_pointer = f"{pointer}/content/{escape(media)}/schema"
        return self.schema_problems(f"{where} body", schema_pointer, response.json())


@cache
def contract(provider: str) -> Contract:
    return Contract(provider)
