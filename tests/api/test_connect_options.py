"""Static keyword discoverability never replaces runtime configuration validation."""
from dataclasses import fields
import inspect
from typing import get_args, get_type_hints

import pytest

from okto_grafx import ConnectOptions, DatabaseConfig, PortRegistry, connect
from okto_grafx.errors import GrafxConfigurationError
from okto_grafx.runtime import config


def test_all_config_keywords_present_and_optional():
    assert set(get_type_hints(ConnectOptions)) == {f.name for f in fields(DatabaseConfig)} - {"path"}
    assert ConnectOptions.__required_keys__ == frozenset()
    assert inspect.signature(connect).parameters["options"].kind is inspect.Parameter.VAR_KEYWORD


@pytest.mark.parametrize("name,values", [
    ("recovery_policy", config.RECOVERY_POLICIES), ("metrics", config.METRICS_SINKS),
    ("codec", config.PAGE_CODEC_SELECTORS), ("vector_math", config.VECTOR_MATH_SELECTORS),
    ("checksum", config.CHECKSUM_SELECTORS),
    ("descriptor_revalidation", config.DESCRIPTOR_REVALIDATION_MODES),
])
def test_selector_types_match_runtime_authority(name, values):
    assert set(get_args(get_type_hints(ConnectOptions)[name])) == set(values)


def test_typed_dictionary_and_unknown_invalid_keywords_keep_runtime_contract():
    options: ConnectOptions = {"page_size": 4096, "descriptor_revalidation": "generation"}
    with connect(":memory:", **options) as db:
        assert db.execute("RETURN 7").rows == ((7,),)
    with pytest.raises(GrafxConfigurationError):
        connect(":memory:", typo_page_size=4096)  # type: ignore[call-arg]
    with pytest.raises(GrafxConfigurationError):
        connect(":memory:", page_size="4096")  # type: ignore[arg-type]


def test_registry_still_is_a_separate_keyword():
    parameter = inspect.signature(connect).parameters["registry"]
    assert parameter.default is None
    assert "registry" not in ConnectOptions.__annotations__
    assert get_type_hints(connect)["registry"] == PortRegistry | None
