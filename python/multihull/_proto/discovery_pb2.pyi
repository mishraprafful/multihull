import datetime

from google.protobuf import timestamp_pb2 as _timestamp_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class DegradedReason(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    DEGRADED_REASON_UNSPECIFIED: _ClassVar[DegradedReason]
    DEGRADED_REASON_QUEUE_DEPTH: _ClassVar[DegradedReason]
    DEGRADED_REASON_TTFT_P95: _ClassVar[DegradedReason]
    DEGRADED_REASON_CAPACITY_ERRORS: _ClassVar[DegradedReason]

class Protocol(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    PROTOCOL_UNSPECIFIED: _ClassVar[Protocol]
    PROTOCOL_HTTP: _ClassVar[Protocol]
    PROTOCOL_SSE: _ClassVar[Protocol]
    PROTOCOL_WEBSOCKET: _ClassVar[Protocol]
    PROTOCOL_GRPC: _ClassVar[Protocol]

class FailoverPolicy(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    FAILOVER_POLICY_UNSPECIFIED: _ClassVar[FailoverPolicy]
    FAILOVER_POLICY_PRIORITY: _ClassVar[FailoverPolicy]
    FAILOVER_POLICY_WEIGHTED: _ClassVar[FailoverPolicy]
    FAILOVER_POLICY_LATENCY: _ClassVar[FailoverPolicy]

class StickyMode(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    STICKY_MODE_UNSPECIFIED: _ClassVar[StickyMode]
    STICKY_MODE_ENDPOINT: _ClassVar[StickyMode]
    STICKY_MODE_PROVIDER: _ClassVar[StickyMode]

class StickyOnUnhealthy(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    STICKY_ON_UNHEALTHY_UNSPECIFIED: _ClassVar[StickyOnUnhealthy]
    STICKY_ON_UNHEALTHY_REHOME: _ClassVar[StickyOnUnhealthy]
    STICKY_ON_UNHEALTHY_FAIL: _ClassVar[StickyOnUnhealthy]

class EndpointType(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    ENDPOINT_TYPE_UNSPECIFIED: _ClassVar[EndpointType]
    ENDPOINT_TYPE_KUBERNETES: _ClassVar[EndpointType]
    ENDPOINT_TYPE_MODAL: _ClassVar[EndpointType]
    ENDPOINT_TYPE_RUNPOD: _ClassVar[EndpointType]
    ENDPOINT_TYPE_BASETEN: _ClassVar[EndpointType]
    ENDPOINT_TYPE_REPLICATE: _ClassVar[EndpointType]
    ENDPOINT_TYPE_DOCKER: _ClassVar[EndpointType]

class Health(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    HEALTH_UNSPECIFIED: _ClassVar[Health]
    HEALTH_READY: _ClassVar[Health]
    HEALTH_DEGRADED: _ClassVar[Health]
    HEALTH_DRAINING: _ClassVar[Health]
    HEALTH_DOWN: _ClassVar[Health]
DEGRADED_REASON_UNSPECIFIED: DegradedReason
DEGRADED_REASON_QUEUE_DEPTH: DegradedReason
DEGRADED_REASON_TTFT_P95: DegradedReason
DEGRADED_REASON_CAPACITY_ERRORS: DegradedReason
PROTOCOL_UNSPECIFIED: Protocol
PROTOCOL_HTTP: Protocol
PROTOCOL_SSE: Protocol
PROTOCOL_WEBSOCKET: Protocol
PROTOCOL_GRPC: Protocol
FAILOVER_POLICY_UNSPECIFIED: FailoverPolicy
FAILOVER_POLICY_PRIORITY: FailoverPolicy
FAILOVER_POLICY_WEIGHTED: FailoverPolicy
FAILOVER_POLICY_LATENCY: FailoverPolicy
STICKY_MODE_UNSPECIFIED: StickyMode
STICKY_MODE_ENDPOINT: StickyMode
STICKY_MODE_PROVIDER: StickyMode
STICKY_ON_UNHEALTHY_UNSPECIFIED: StickyOnUnhealthy
STICKY_ON_UNHEALTHY_REHOME: StickyOnUnhealthy
STICKY_ON_UNHEALTHY_FAIL: StickyOnUnhealthy
ENDPOINT_TYPE_UNSPECIFIED: EndpointType
ENDPOINT_TYPE_KUBERNETES: EndpointType
ENDPOINT_TYPE_MODAL: EndpointType
ENDPOINT_TYPE_RUNPOD: EndpointType
ENDPOINT_TYPE_BASETEN: EndpointType
ENDPOINT_TYPE_REPLICATE: EndpointType
ENDPOINT_TYPE_DOCKER: EndpointType
HEALTH_UNSPECIFIED: Health
HEALTH_READY: Health
HEALTH_DEGRADED: Health
HEALTH_DRAINING: Health
HEALTH_DOWN: Health

class RouterMessage(_message.Message):
    __slots__ = ("hello", "ack", "nack", "degraded")
    HELLO_FIELD_NUMBER: _ClassVar[int]
    ACK_FIELD_NUMBER: _ClassVar[int]
    NACK_FIELD_NUMBER: _ClassVar[int]
    DEGRADED_FIELD_NUMBER: _ClassVar[int]
    hello: Hello
    ack: Ack
    nack: Nack
    degraded: Degraded
    def __init__(self, hello: _Optional[_Union[Hello, _Mapping]] = ..., ack: _Optional[_Union[Ack, _Mapping]] = ..., nack: _Optional[_Union[Nack, _Mapping]] = ..., degraded: _Optional[_Union[Degraded, _Mapping]] = ...) -> None: ...

class ControlMessage(_message.Message):
    __slots__ = ("snapshot",)
    SNAPSHOT_FIELD_NUMBER: _ClassVar[int]
    snapshot: Snapshot
    def __init__(self, snapshot: _Optional[_Union[Snapshot, _Mapping]] = ...) -> None: ...

class Hello(_message.Message):
    __slots__ = ("node_id", "last_version")
    NODE_ID_FIELD_NUMBER: _ClassVar[int]
    LAST_VERSION_FIELD_NUMBER: _ClassVar[int]
    node_id: str
    last_version: int
    def __init__(self, node_id: _Optional[str] = ..., last_version: _Optional[int] = ...) -> None: ...

class Ack(_message.Message):
    __slots__ = ("version",)
    VERSION_FIELD_NUMBER: _ClassVar[int]
    version: int
    def __init__(self, version: _Optional[int] = ...) -> None: ...

class Nack(_message.Message):
    __slots__ = ("version", "reason")
    VERSION_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    version: int
    reason: str
    def __init__(self, version: _Optional[int] = ..., reason: _Optional[str] = ...) -> None: ...

class Degraded(_message.Message):
    __slots__ = ("service", "provider", "reason", "observed_concurrency")
    SERVICE_FIELD_NUMBER: _ClassVar[int]
    PROVIDER_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    OBSERVED_CONCURRENCY_FIELD_NUMBER: _ClassVar[int]
    service: str
    provider: str
    reason: DegradedReason
    observed_concurrency: int
    def __init__(self, service: _Optional[str] = ..., provider: _Optional[str] = ..., reason: _Optional[_Union[DegradedReason, str]] = ..., observed_concurrency: _Optional[int] = ...) -> None: ...

class Snapshot(_message.Message):
    __slots__ = ("version", "at", "routes")
    VERSION_FIELD_NUMBER: _ClassVar[int]
    AT_FIELD_NUMBER: _ClassVar[int]
    ROUTES_FIELD_NUMBER: _ClassVar[int]
    version: int
    at: _timestamp_pb2.Timestamp
    routes: _containers.RepeatedCompositeFieldContainer[Route]
    def __init__(self, version: _Optional[int] = ..., at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., routes: _Optional[_Iterable[_Union[Route, _Mapping]]] = ...) -> None: ...

class Route(_message.Message):
    __slots__ = ("id", "hostname", "path_prefix", "protocol", "failover", "auth", "sticky", "endpoints")
    ID_FIELD_NUMBER: _ClassVar[int]
    HOSTNAME_FIELD_NUMBER: _ClassVar[int]
    PATH_PREFIX_FIELD_NUMBER: _ClassVar[int]
    PROTOCOL_FIELD_NUMBER: _ClassVar[int]
    FAILOVER_FIELD_NUMBER: _ClassVar[int]
    AUTH_FIELD_NUMBER: _ClassVar[int]
    STICKY_FIELD_NUMBER: _ClassVar[int]
    ENDPOINTS_FIELD_NUMBER: _ClassVar[int]
    id: str
    hostname: str
    path_prefix: str
    protocol: Protocol
    failover: Failover
    auth: Auth
    sticky: Sticky
    endpoints: _containers.RepeatedCompositeFieldContainer[Endpoint]
    def __init__(self, id: _Optional[str] = ..., hostname: _Optional[str] = ..., path_prefix: _Optional[str] = ..., protocol: _Optional[_Union[Protocol, str]] = ..., failover: _Optional[_Union[Failover, _Mapping]] = ..., auth: _Optional[_Union[Auth, _Mapping]] = ..., sticky: _Optional[_Union[Sticky, _Mapping]] = ..., endpoints: _Optional[_Iterable[_Union[Endpoint, _Mapping]]] = ...) -> None: ...

class Failover(_message.Message):
    __slots__ = ("policy", "retry_on", "max_retries")
    POLICY_FIELD_NUMBER: _ClassVar[int]
    RETRY_ON_FIELD_NUMBER: _ClassVar[int]
    MAX_RETRIES_FIELD_NUMBER: _ClassVar[int]
    policy: FailoverPolicy
    retry_on: _containers.RepeatedScalarFieldContainer[str]
    max_retries: int
    def __init__(self, policy: _Optional[_Union[FailoverPolicy, str]] = ..., retry_on: _Optional[_Iterable[str]] = ..., max_retries: _Optional[int] = ...) -> None: ...

class Auth(_message.Message):
    __slots__ = ("api_key_hashes", "required")
    API_KEY_HASHES_FIELD_NUMBER: _ClassVar[int]
    REQUIRED_FIELD_NUMBER: _ClassVar[int]
    api_key_hashes: _containers.RepeatedScalarFieldContainer[str]
    required: bool
    def __init__(self, api_key_hashes: _Optional[_Iterable[str]] = ..., required: _Optional[bool] = ...) -> None: ...

class Sticky(_message.Message):
    __slots__ = ("key", "ttl_seconds", "mode", "on_unhealthy", "fallback_key")
    KEY_FIELD_NUMBER: _ClassVar[int]
    TTL_SECONDS_FIELD_NUMBER: _ClassVar[int]
    MODE_FIELD_NUMBER: _ClassVar[int]
    ON_UNHEALTHY_FIELD_NUMBER: _ClassVar[int]
    FALLBACK_KEY_FIELD_NUMBER: _ClassVar[int]
    key: str
    ttl_seconds: int
    mode: StickyMode
    on_unhealthy: StickyOnUnhealthy
    fallback_key: str
    def __init__(self, key: _Optional[str] = ..., ttl_seconds: _Optional[int] = ..., mode: _Optional[_Union[StickyMode, str]] = ..., on_unhealthy: _Optional[_Union[StickyOnUnhealthy, str]] = ..., fallback_key: _Optional[str] = ...) -> None: ...

class Endpoint(_message.Message):
    __slots__ = ("id", "provider", "type", "url", "region", "priority", "weight", "health", "ready_replicas", "max_concurrency", "inject_headers", "health_path", "edge_error")
    class InjectHeadersEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: str
        def __init__(self, key: _Optional[str] = ..., value: _Optional[str] = ...) -> None: ...
    ID_FIELD_NUMBER: _ClassVar[int]
    PROVIDER_FIELD_NUMBER: _ClassVar[int]
    TYPE_FIELD_NUMBER: _ClassVar[int]
    URL_FIELD_NUMBER: _ClassVar[int]
    REGION_FIELD_NUMBER: _ClassVar[int]
    PRIORITY_FIELD_NUMBER: _ClassVar[int]
    WEIGHT_FIELD_NUMBER: _ClassVar[int]
    HEALTH_FIELD_NUMBER: _ClassVar[int]
    READY_REPLICAS_FIELD_NUMBER: _ClassVar[int]
    MAX_CONCURRENCY_FIELD_NUMBER: _ClassVar[int]
    INJECT_HEADERS_FIELD_NUMBER: _ClassVar[int]
    HEALTH_PATH_FIELD_NUMBER: _ClassVar[int]
    EDGE_ERROR_FIELD_NUMBER: _ClassVar[int]
    id: str
    provider: str
    type: EndpointType
    url: str
    region: str
    priority: int
    weight: int
    health: Health
    ready_replicas: int
    max_concurrency: int
    inject_headers: _containers.ScalarMap[str, str]
    health_path: str
    edge_error: EdgeError
    def __init__(self, id: _Optional[str] = ..., provider: _Optional[str] = ..., type: _Optional[_Union[EndpointType, str]] = ..., url: _Optional[str] = ..., region: _Optional[str] = ..., priority: _Optional[int] = ..., weight: _Optional[int] = ..., health: _Optional[_Union[Health, str]] = ..., ready_replicas: _Optional[int] = ..., max_concurrency: _Optional[int] = ..., inject_headers: _Optional[_Mapping[str, str]] = ..., health_path: _Optional[str] = ..., edge_error: _Optional[_Union[EdgeError, _Mapping]] = ...) -> None: ...

class EdgeError(_message.Message):
    __slots__ = ("statuses", "body_prefix")
    STATUSES_FIELD_NUMBER: _ClassVar[int]
    BODY_PREFIX_FIELD_NUMBER: _ClassVar[int]
    statuses: _containers.RepeatedScalarFieldContainer[int]
    body_prefix: str
    def __init__(self, statuses: _Optional[_Iterable[int]] = ..., body_prefix: _Optional[str] = ...) -> None: ...
