"""Closed provider registry validated against packaged, versioned definitions."""

import tomllib
from importlib.resources import files

NAMES = frozenset({"generic_ir", "sec_edgar", "hkexnews"})


def definitions():
    root = files(__package__).joinpath("definitions")
    result = {
        p.name.removesuffix(".toml"): tomllib.loads(p.read_text())
        for p in root.iterdir()
        if p.name.endswith(".toml")
    }
    if result.keys() != NAMES:
        raise ValueError("provider definitions disagree with the registration map")
    for definition in result.values():
        if definition.get("schema") != 1 or definition["budget"] < 1 or definition["pace_ms"] < 0:
            raise ValueError("unsupported provider definition")
    return result
