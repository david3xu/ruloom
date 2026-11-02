"""Schematic shared scene/image registration (R03, RP-06).

One shared scene record binds a tabular sample row to its image
region: each entry carries a ``sample_id``, a depth interval
(``depth_top``/``depth_bottom`` metres), the source image name
(``image_id``), a pixel row band (``row_lo``/``row_hi``, upper
inclusive-exclusive over the image height), and an ``orientation``.

Scope is deliberately schematic-only: the pixel bands are the
deterministic strip layout our own encoder writes (``N_STRIPS``
contiguous row bands), and the depths are plan/schematic intervals,
not measured core-photo alignment. Real NVCL core-photo registration
is unclaimed: no core photograph is inventoried here, no pixel is
fitted to a photographed tray, and nothing in this module measures
sub-pixel or cross-image correspondence. The scene proves the two
views agree with each other (same samples, same order, same
mapping), never that either view matches a real core photograph.

Both views derive from the same entries, so agreement is structural:

* :func:`table_view` -- one row per entry: sample id + depth interval;
* :func:`image_view` -- one band per entry: sample id + pixel region
  + orientation;
* :func:`views_agree` -- both views name the same samples in the same
  order with the same mapping.

:func:`validate_scene` rejects every registration defect loudly: a
missing region (zero-height band), an out-of-bounds band, a duplicate
or missing/empty sample id, wrong row order (unsorted or overlapping
bands), a wrong orientation, and any gap or overlap in the
``[0, height)`` coverage.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

#: The only supported schematic orientation: row 0 is the shallow end.
ORIENTATION_TOP_DOWN = "top_down"


@dataclass(frozen=True)
class SceneEntry:
    """One sample bound to one depth interval and one pixel band."""

    sample_id: str
    depth_top: float
    depth_bottom: float
    image_id: str
    row_lo: int
    row_hi: int
    orientation: str = ORIENTATION_TOP_DOWN

    def to_dict(self) -> dict[str, Any]:
        return {
            "sample_id": self.sample_id,
            "depth_top": self.depth_top,
            "depth_bottom": self.depth_bottom,
            "image_id": self.image_id,
            "row_lo": self.row_lo,
            "row_hi": self.row_hi,
            "orientation": self.orientation,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SceneEntry:
        return cls(
            sample_id=str(data["sample_id"]),
            depth_top=float(data["depth_top"]),
            depth_bottom=float(data["depth_bottom"]),
            image_id=str(data["image_id"]),
            row_lo=int(data["row_lo"]),
            row_hi=int(data["row_hi"]),
            orientation=str(data.get("orientation", ORIENTATION_TOP_DOWN)),
        )


@dataclass(frozen=True)
class Scene:
    """The shared registration: every entry in one ordered record."""

    image_id: str
    width: int
    height: int
    entries: tuple[SceneEntry, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "image_id": self.image_id,
            "width": self.width,
            "height": self.height,
            "orientation": ORIENTATION_TOP_DOWN,
            "schematic_only": True,
            "entries": [entry.to_dict() for entry in self.entries],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Scene:
        return cls(
            image_id=str(data["image_id"]),
            width=int(data["width"]),
            height=int(data["height"]),
            entries=tuple(
                SceneEntry.from_dict(item) for item in data.get("entries", [])
            ),
        )


def build_schematic_scene(
    reference_stats: Mapping[str, Any],
    image_id: str,
    *,
    depth_start: float = 0.0,
    depth_step: float = 1.0,
    sample_ids: Sequence[str] | None = None,
) -> Scene:
    """Build the schematic scene for one reference image.

    One entry per statistics strip: the pixel band comes from the
    strip's own ``row_lo``/``row_hi`` (the encoder's contiguous
    layout), and the depth interval partitions
    ``[depth_start, depth_start + n_strips * depth_step)`` contiguously
    in strip order. Sample ids default to ``sample-000`` ...;
    callers with real sample names pass them explicitly. Width, height
    and image id are recorded so later validation can re-check bounds.
    """
    strips = list(reference_stats["strips"])
    if not strips:
        raise ValueError("reference statistics carry no strips")
    width = int(reference_stats["width"])
    height = int(reference_stats["height"])
    names = (
        list(sample_ids)
        if sample_ids is not None
        else [f"sample-{index:03d}" for index in range(len(strips))]
    )
    if len(names) != len(strips):
        raise ValueError(f"need {len(strips)} sample ids, got {len(names)}")
    entries: list[SceneEntry] = []
    for index, strip in enumerate(strips):
        entries.append(
            SceneEntry(
                sample_id=names[index],
                depth_top=float(depth_start + index * depth_step),
                depth_bottom=float(depth_start + (index + 1) * depth_step),
                image_id=image_id,
                row_lo=int(strip["row_lo"]),
                row_hi=int(strip["row_hi"]),
                orientation=ORIENTATION_TOP_DOWN,
            )
        )
    return Scene(image_id=image_id, width=width, height=height, entries=tuple(entries))


def validate_scene(scene: Scene | Mapping[str, Any]) -> list[str]:
    """Return one error string per registration defect (empty when valid).

    Checks, in order: entries present; every band has positive height
    (missing region); every band lies inside ``[0, height)``
    (out-of-bounds); sample ids are non-empty, unique, and gap-free in
    count (duplicate/missing); row bands are strictly increasing in
    entry order (wrong order) with no overlap; every orientation is
    ``top_down`` (wrong orientation); the bands cover ``[0, height)``
    exactly with no gap and no overlap.
    """
    record = scene if isinstance(scene, Scene) else Scene.from_dict(scene)
    errors: list[str] = []
    if not record.entries:
        return ["scene carries no entries: missing region mapping"]
    seen: set[str] = set()
    previous_hi: int | None = None
    for position, entry in enumerate(record.entries):
        tag = f"entry {position} ({entry.sample_id!r})"
        if not entry.sample_id:
            errors.append(f"{tag} carries an empty sample id: missing sample")
        elif entry.sample_id in seen:
            errors.append(f"{tag} repeats sample id {entry.sample_id!r}: duplicate")
        else:
            seen.add(entry.sample_id)
        if entry.row_hi <= entry.row_lo:
            errors.append(
                f"{tag} covers rows [{entry.row_lo}, {entry.row_hi}): missing region"
            )
        if entry.row_lo < 0 or entry.row_hi > record.height:
            errors.append(
                f"{tag} covers rows [{entry.row_lo}, {entry.row_hi}) "
                f"outside image height {record.height}: out-of-bounds"
            )
        if entry.orientation != ORIENTATION_TOP_DOWN:
            errors.append(
                f"{tag} orientation {entry.orientation!r} is not "
                f"{ORIENTATION_TOP_DOWN!r}: wrong orientation"
            )
        if entry.depth_bottom <= entry.depth_top:
            errors.append(
                f"{tag} depth interval "
                f"[{entry.depth_top}, {entry.depth_bottom}] is empty: "
                "missing region"
            )
        if previous_hi is not None and entry.row_lo < previous_hi:
            errors.append(
                f"{tag} starts at row {entry.row_lo} before the previous "
                f"band ends at row {previous_hi}: wrong order/overlap"
            )
        previous_hi = (
            entry.row_hi if previous_hi is None else max(previous_hi, entry.row_hi)
        )
    ordered_lo = [entry.row_lo for entry in record.entries]
    if ordered_lo != sorted(ordered_lo):
        errors.append("scene bands are not in increasing row order")
    cursor = 0
    for entry in sorted(record.entries, key=lambda item: item.row_lo):
        if entry.row_lo != cursor:
            errors.append(
                f"coverage gap/overlap: band starts at row {entry.row_lo}, "
                f"expected {cursor}"
            )
            break
        cursor = entry.row_hi
    else:
        if cursor != record.height:
            errors.append(
                f"coverage ends at row {cursor}, image height is "
                f"{record.height}: missing region"
            )
    return errors


def table_view(scene: Scene | Mapping[str, Any]) -> list[dict[str, Any]]:
    """Tabular view of the scene: sample ids with depth intervals."""
    record = scene if isinstance(scene, Scene) else Scene.from_dict(scene)
    return [
        {
            "sample_id": entry.sample_id,
            "depth_top": entry.depth_top,
            "depth_bottom": entry.depth_bottom,
        }
        for entry in record.entries
    ]


def image_view(scene: Scene | Mapping[str, Any]) -> list[dict[str, Any]]:
    """Image view of the scene: sample ids with pixel bands."""
    record = scene if isinstance(scene, Scene) else Scene.from_dict(scene)
    return [
        {
            "sample_id": entry.sample_id,
            "image_id": entry.image_id,
            "row_lo": entry.row_lo,
            "row_hi": entry.row_hi,
            "orientation": entry.orientation,
        }
        for entry in record.entries
    ]


def views_agree(scene: Scene | Mapping[str, Any]) -> bool:
    """Both views name the same samples in the same order."""
    table = table_view(scene)
    image = image_view(scene)
    if len(table) != len(image):
        return False
    return all(
        table_row["sample_id"] == image_row["sample_id"]
        for table_row, image_row in zip(table, image)
    )


__all__ = [
    "ORIENTATION_TOP_DOWN",
    "Scene",
    "SceneEntry",
    "build_schematic_scene",
    "image_view",
    "table_view",
    "validate_scene",
    "views_agree",
]
