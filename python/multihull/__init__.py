from importlib.metadata import PackageNotFoundError, version

from multihull.sdk import Service, TargetFactory, target
from multihull.spec import ServiceSpec, load

try:
    __version__ = version("multihull")
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"

__all__ = ["Service", "ServiceSpec", "TargetFactory", "__version__", "load", "target"]
