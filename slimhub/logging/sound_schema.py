from __future__ import annotations

from dataclasses import dataclass


B_TFLM_V1 = "b-tflm-v1"


@dataclass(frozen=True)
class SoundSchema:
    version: str
    labels: tuple[str, ...]

    @property
    def class_count(self) -> int:
        return len(self.labels)


# DEAN Node B TFLM currently emits ten scores.  In particular index 8/9 are
# watering_low/high, not microwave/cooking.
B_TFLM_V1_SCHEMA = SoundSchema(
    version=B_TFLM_V1,
    labels=(
        "background",
        "hitting",
        "speech_tv",
        "air_appliances",
        "brushing",
        "peeing",
        "flushing",
        "flush_end",
        "watering_low",
        "watering_high",
    ),
)

SCHEMAS = {B_TFLM_V1: B_TFLM_V1_SCHEMA}


def resolve_sound_schema(version: str | None, class_count: int | None = None) -> SoundSchema:
    schema = SCHEMAS.get((version or "").strip().lower(), B_TFLM_V1_SCHEMA)
    if class_count is not None and class_count != schema.class_count:
        raise ValueError(
            f"sound schema {schema.version!r} expects {schema.class_count} classes, got {class_count}"
        )
    return schema
