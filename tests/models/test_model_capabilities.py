"""Tests for ModelCapabilities and adapter capability declarations.

Verifies default text-only bounds, strict type and spelling validation,
consistency between modalities and bounds, immutability, and presence of
public .capabilities across all adapters.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from models.agy_cli import AgyCLIModel
from models.azure_foundry import AzureFoundryModel
from models.base import ModelCapabilities
from models.glimmer import GlimmerLocalModel
from models.meta_cli import MuseCLIModel

VALID_URL = "https://audit-test.openai.azure.com/openai/v1/"
VALID_SUB = "11111111-2222-3333-4444-555555555555"


def test_capabilities_defaults_text_only() -> None:
    caps = ModelCapabilities()
    assert caps.input_modalities == ("text",)
    assert caps.structured_output is True
    assert caps.image_media_types == ()
    assert caps.max_image_bytes == 0
    assert caps.max_image_pixels == 0
    assert caps.supports_image is False


def test_capabilities_immutable() -> None:
    caps = ModelCapabilities()
    with pytest.raises(FrozenInstanceError):
        caps.input_modalities = ("text", "image")  # type: ignore[misc]


def test_capabilities_vision_positive_bounds() -> None:
    caps = ModelCapabilities(
        input_modalities=("text", "image"),
        structured_output=True,
        image_media_types=("image/png",),
        max_image_bytes=4 * 1024 * 1024,
        max_image_pixels=4_000_000,
    )
    assert caps.supports_image is True
    assert caps.max_image_bytes == 4194304
    assert caps.max_image_pixels == 4000000
    assert caps.image_media_types == ("image/png",)


def test_capabilities_rejects_non_bool_structured_output() -> None:
    with pytest.raises(ValueError, match="structured_output must be an exact bool"):
        ModelCapabilities(structured_output="True")  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="structured_output must be an exact bool"):
        ModelCapabilities(structured_output=1)  # type: ignore[arg-type]


def test_capabilities_rejects_unsupported_modality_spelling() -> None:
    with pytest.raises(ValueError, match="unsupported modality 'vision'"):
        ModelCapabilities(input_modalities=("text", "vision"))

    with pytest.raises(ValueError, match="unsupported modality 'IMAGE'"):
        ModelCapabilities(input_modalities=("text", "IMAGE"))


def test_capabilities_rejects_unsupported_media_type() -> None:
    with pytest.raises(ValueError, match="unsupported image media type 'image/jpeg'"):
        ModelCapabilities(
            input_modalities=("text", "image"),
            image_media_types=("image/jpeg",),
            max_image_bytes=1000,
            max_image_pixels=1000,
        )


def test_capabilities_rejects_inconsistent_image_declarations() -> None:
    # Image modality declared with structured_output False
    with pytest.raises(
        ValueError, match="image modality requires structured_output True"
    ):
        ModelCapabilities(
            input_modalities=("text", "image"),
            structured_output=False,
            image_media_types=("image/png",),
            max_image_bytes=1000,
            max_image_pixels=1000,
        )

    # Image modality declared but missing media types
    with pytest.raises(
        ValueError,
        match="image modality requires at least one admitted image_media_type",
    ):
        ModelCapabilities(
            input_modalities=("text", "image"),
            image_media_types=(),
            max_image_bytes=1000,
            max_image_pixels=1000,
        )

    # Image modality declared but 0 max_image_bytes
    with pytest.raises(
        ValueError, match="image modality requires positive max_image_bytes"
    ):
        ModelCapabilities(
            input_modalities=("text", "image"),
            image_media_types=("image/png",),
            max_image_bytes=0,
            max_image_pixels=1000,
        )

    # Text-only declared but image media types provided
    with pytest.raises(
        ValueError,
        match="image_media_types must be empty when 'image' modality is absent",
    ):
        ModelCapabilities(
            input_modalities=("text",),
            image_media_types=("image/png",),
        )

    # Text-only declared but positive bytes provided
    with pytest.raises(
        ValueError, match="max_image_bytes must be 0 when 'image' modality is absent"
    ):
        ModelCapabilities(
            input_modalities=("text",),
            max_image_bytes=1024,
        )


def test_capabilities_validation_rejects_negative_bounds() -> None:
    with pytest.raises(ValueError, match="max_image_bytes"):
        ModelCapabilities(max_image_bytes=-1)

    with pytest.raises(ValueError, match="max_image_pixels"):
        ModelCapabilities(max_image_pixels=-1)


def test_capabilities_coerces_lists_to_tuples() -> None:
    caps = ModelCapabilities(
        input_modalities=["text", "image"],  # type: ignore[arg-type]
        image_media_types=["image/png"],  # type: ignore[arg-type]
        max_image_bytes=1024,
        max_image_pixels=100,
    )
    assert isinstance(caps.input_modalities, tuple)
    assert isinstance(caps.image_media_types, tuple)


def test_agy_cli_capabilities_are_text_only() -> None:
    adapter = AgyCLIModel()
    assert hasattr(adapter, "capabilities")
    assert adapter.capabilities.input_modalities == ("text",)
    assert adapter.capabilities.supports_image is False
    assert adapter.capabilities.image_media_types == ()
    assert adapter.capabilities.max_image_bytes == 0


def test_glimmer_local_capabilities_are_text_only() -> None:
    adapter = GlimmerLocalModel()
    assert hasattr(adapter, "capabilities")
    assert adapter.capabilities.input_modalities == ("text",)
    assert adapter.capabilities.supports_image is False
    assert adapter.capabilities.image_media_types == ()
    assert adapter.capabilities.max_image_bytes == 0


def test_azure_foundry_spark_capabilities_are_text_only() -> None:
    adapter = AzureFoundryModel(
        base_url=VALID_URL,
        subscription_id=VALID_SUB,
        model="muse-spark-1.3",
    )
    assert hasattr(adapter, "capabilities")
    assert adapter.capabilities.input_modalities == ("text",)
    assert adapter.capabilities.supports_image is False
    assert adapter.capabilities.image_media_types == ()
    assert adapter.capabilities.max_image_bytes == 0
    assert adapter.capabilities.max_image_pixels == 0


def test_azure_foundry_glimmer_capabilities_declare_vision() -> None:
    adapter = AzureFoundryModel(
        base_url=VALID_URL,
        subscription_id=VALID_SUB,
        model="muse-glimmer-30b",
    )
    assert hasattr(adapter, "capabilities")
    assert "image" in adapter.capabilities.input_modalities
    assert adapter.capabilities.image_media_types == ("image/png",)
    assert adapter.capabilities.max_image_bytes == 4 * 1024 * 1024
    assert adapter.capabilities.max_image_pixels == 4_000_000
    assert adapter.capabilities.supports_image is True


def test_muse_cli_capabilities_scoped_to_verified_spark() -> None:
    spark_adapter = MuseCLIModel(model="muse-spark-1.3")
    assert spark_adapter.capabilities.supports_image is True
    assert "image" in spark_adapter.capabilities.input_modalities

    other_adapter = MuseCLIModel(model="arbitrary-unverified-model")
    assert other_adapter.capabilities.supports_image is False
    assert other_adapter.capabilities.input_modalities == ("text",)
