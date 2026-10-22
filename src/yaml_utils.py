"""Safe YAML loading that reports the file, line and key when something is wrong."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from src.errors import ConfigError


class _UniqueKeyLoader(yaml.SafeLoader):
    """`safe_load` that refuses a repeated key instead of silently keeping the last one.

    A copy-paste slip such as two `alice:` entries or two `guards:` sections would otherwise change a
    user's brand scope or a privacy threshold without any warning.
    """

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict:
        seen: set = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                if key in seen:
                    raise yaml.constructor.ConstructorError(
                        None, None, f"found a duplicate key {key!r}", key_node.start_mark
                    )
                seen.add(key)
            except TypeError:
                pass  # unhashable key: the parent class reports it
        return super().construct_mapping(node, deep=deep)


def decode_utf8(raw: bytes, path: Path, what: str = "file") -> str:
    """Strict UTF-8 (a leading byte order mark is fine). Anything else is a clear ConfigError."""
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ConfigError(
            f"The {what} is not UTF-8 text: {path}",
            fix="Save the file as UTF-8 (a file written by PowerShell's > is UTF-16).",
        ) from None


def read_text(path: Path, what: str = "file") -> str:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raise ConfigError(f"The {what} was not found: {path}") from None
    except OSError as exc:
        raise ConfigError(f"The {what} could not be read: {path} ({exc.strerror or exc})") from None
    return decode_utf8(raw, path, what)


def load_yaml(path: Path, what: str = "file") -> Any:
    """Parse a YAML file. An empty file gives `{}`."""
    return parse_yaml(read_text(path, what), path)


def parse_yaml(text: str, path: Path) -> Any:
    """Like `load_yaml` for text already read. `path` is only used in messages."""
    try:
        data = yaml.load(text, Loader=_UniqueKeyLoader)  # noqa: S506 - a SafeLoader subclass
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        problem = getattr(exc, "problem", None) or str(exc)
        where = f" line {mark.line + 1}" if mark is not None else ""
        raise ConfigError(f"{path.name}{where}: {problem}", fix=f"Fix the YAML in {path}.") from None
    except (ValueError, TypeError, OverflowError) as exc:  # for example the date 2020-13-45
        raise ConfigError(f"{path.name}: a value could not be read ({exc})", fix=f"Fix the YAML in {path}.") from None
    return {} if data is None else data


def line_of(path: Path, loc: tuple[Any, ...]) -> int | None:
    """Line number (1-based) of the key at `loc` (for example ('loop', 'max_replans')), if it exists."""
    try:
        node = yaml.compose(path.read_text(encoding="utf-8-sig"), Loader=yaml.SafeLoader)
    except (OSError, ValueError, yaml.YAMLError):  # ValueError includes UnicodeDecodeError
        return None
    last = None
    for part in loc:
        if isinstance(node, yaml.MappingNode):
            for key_node, value_node in node.value:
                if key_node.value == str(part):
                    last, node = key_node, value_node
                    break
            else:
                break
        elif isinstance(node, yaml.SequenceNode) and isinstance(part, int) and part < len(node.value):
            last = node = node.value[part]
        else:
            break
    return None if last is None else last.start_mark.line + 1


def validation_message(path: Path, exc: ValidationError) -> str:
    """One readable line per problem: `file line N: key.path: what is wrong`."""
    lines = []
    for err in exc.errors(include_url=False):
        loc = tuple(err["loc"])
        line = line_of(path, loc)
        key = ".".join(str(p) for p in loc) or "(top level)"
        msg = "unknown key" if err["type"] == "extra_forbidden" else err["msg"]
        lines.append(f"{path.name}{f' line {line}' if line else ''}: {key}: {msg}")
    return "\n".join(lines)
