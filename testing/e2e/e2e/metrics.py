from __future__ import annotations

import re
from dataclasses import dataclass, field

SAMPLE = re.compile(r"^([A-Za-z_:][A-Za-z0-9_:]*)(?:\{(.*)\})?\s+(\S+)")
LABEL = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)="((?:[^"\\]|\\.)*)"')


@dataclass
class Sample:
    name: str
    labels: dict[str, str]
    value: float


@dataclass
class Metrics:
    samples: list[Sample] = field(default_factory=list)

    @classmethod
    def parse(cls, text: str) -> Metrics:
        samples: list[Sample] = []
        for line in text.splitlines():
            if not line or line.startswith("#"):
                continue
            match = SAMPLE.match(line)
            if match is None:
                continue
            name, raw_labels, raw_value = match.groups()
            labels = {key: value for key, value in LABEL.findall(raw_labels or "")}
            samples.append(Sample(name, labels, float(raw_value)))
        return cls(samples)

    def series(self, name: str, **labels: str) -> list[Sample]:
        return [
            sample
            for sample in self.samples
            if sample.name == name
            and all(sample.labels.get(key) == value for key, value in labels.items())
        ]

    def total(self, name: str, **labels: str) -> float:
        return sum(sample.value for sample in self.series(name, **labels))

    def gauge(self, name: str, **labels: str) -> float | None:
        matched = self.series(name, **labels)
        return matched[0].value if matched else None

    def failovers(self, **labels: str) -> float:
        return self.total("router_failovers_total", **labels)

    def requests(self, **labels: str) -> float:
        return self.total("router_requests_total", **labels)

    def circuit_state(self, endpoint: str) -> float | None:
        return self.gauge("router_circuit_state", endpoint=endpoint)

    def sticky(self, outcome: str) -> float:
        return self.total("router_sticky_requests_total", outcome=outcome)
