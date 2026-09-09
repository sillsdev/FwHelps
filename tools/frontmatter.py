"""Shared helpers for safely rendering YAML front matter values."""

from __future__ import annotations

import json


def yaml_scalar(value: object) -> str:
    """Render a scalar as a JSON-quoted YAML-compatible string."""
    return json.dumps(str(value), ensure_ascii=False)


__all__ = ["yaml_scalar"]
