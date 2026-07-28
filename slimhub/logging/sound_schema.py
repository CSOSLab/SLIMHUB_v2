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
        "flushing_end",
        "watering_low",
        "watering_high",
    ),
)

KITCHEN_V1_SCHEMA = SoundSchema(
    version="kitchen_v1",
    labels=(
        "background",
        "hitting",
        "speech_tv",
        "air_appliances",
        "cooking",
        "microwave",
        "watering_low",
        "watering_high",
        "appliances",
    ),
)

LIVING_V1_SCHEMA = SoundSchema(
    version="living_v1",
    labels=(
        "background",
        "hitting",
        "speech_tv",
        "air_appliances",
        "snoring",
    ),
)

TOILET_V1_SCHEMA = SoundSchema(
    version="toilet_v1",
    labels=B_TFLM_V1_SCHEMA.labels,
)

SCHEMAS = {
    B_TFLM_V1: B_TFLM_V1_SCHEMA,
    "toilet_v1": TOILET_V1_SCHEMA,
    "kitchen_v1": KITCHEN_V1_SCHEMA,
    "living_v1": LIVING_V1_SCHEMA,
}


def resolve_sound_schema(version: str | None, class_count: int | None = None) -> SoundSchema:
    schema = SCHEMAS.get((version or "").strip().lower(), B_TFLM_V1_SCHEMA)
    if class_count is not None and class_count != schema.class_count:
        raise ValueError(
            f"sound schema {schema.version!r} expects {schema.class_count} classes, got {class_count}"
        )
    return schema


def resolve_node_sound_schema(
    *,
    profile: str | None,
    location: str | None,
    class_count: int | None,
    semantic: object = "1",
) -> SoundSchema | None:
    """Resolve labels only when Node metadata says semantic output is safe."""
    semantic_text = str(semantic or "").strip().lower()
    if semantic_text in {"", "0", "false", "disabled", "not_ready"}:
        return None
    profile_key = str(profile or "").strip().lower()
    if not profile_key:
        room = str(location or "").strip().upper()
        profile_key = {
            "TOILET": "toilet_v1",
            "KITCHEN": "kitchen_v1",
            "LIVING": "living_v1",
            "BEDROOM": "living_v1",
        }.get(room, "")
    schema = SCHEMAS.get(profile_key)
    if schema is None:
        return None
    if class_count is None or not 1 <= class_count <= 16:
        return None
    if class_count != schema.class_count:
        return None
    return schema
