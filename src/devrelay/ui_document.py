"""Small, loss-conscious editor for DevRelay's generated TOML layout.

Unusual TOML layouts remain editable through the source editor. Never silently
normalize an entire document or interpret a command string as a shell command.
This module is also loaded by the isolated runtime bridge.
"""

import copy
import json
import re
import tomllib


def literal(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, int, list)):
        # TOML strings/arrays share this JSON subset (no nulls or objects).
        if isinstance(value, list):
            return "[" + ", ".join(literal(item) for item in value) + "]"
        return json.dumps(value, ensure_ascii=False)
    raise ValueError("Unsupported configuration value")


def statements(source):
    lines = source.splitlines(keepends=True)
    result, start = [], 0
    while start < len(lines):
        if not lines[start].strip() or lines[start].lstrip().startswith("#"):
            result.append(("comment", None, lines[start]))
            start += 1
            continue
        for end in range(start + 1, len(lines) + 1):
            text = "".join(lines[start:end])
            try:
                parsed = tomllib.loads(text)
            except tomllib.TOMLDecodeError:
                continue
            header = re.fullmatch(r"\s*(\[\[?)([a-z_]+)\]\]?\s*(?:#[^\n]*)?\s*", text)
            key = re.match(r"\s*([a-z_]+)\s*=", text)
            if header:
                result.append(("array" if header[1] == "[[" else "table", header[2], text))
            elif key and set(parsed) == {key[1]}:
                result.append(("value", key[1], text))
            else:
                raise ValueError("This TOML layout needs the source editor (源码编辑)")
            start = end
            break
        else:
            raise ValueError("Cannot locate a complete TOML statement; use the source editor")
    return result


def trailing_comment(text):
    parsed = tomllib.loads(text)
    for index, char in enumerate(text):
        if char == "#":
            try:
                if tomllib.loads(text[:index]) == parsed:
                    return " " + text[index:].rstrip("\r\n")
            except tomllib.TOMLDecodeError:
                pass
    return ""


def update(source, changes):
    original = tomllib.loads(source)
    expected = copy.deepcopy(original)
    scalar, arrays = {}, {}
    for path, value in changes.items():
        parts = path.split(".")
        if len(parts) == 1 and path in ("setup", "checks"):
            if not isinstance(value, list):
                raise ValueError(f"{path} must be a command array")
            expected[path] = value
            arrays[path] = value
        elif len(parts) == 2 and parts[0] in ("project", "queue", "codex", "delivery", "discovery"):
            section, key = parts
            if value is None:
                expected.setdefault(section, {}).pop(key, None)
            else:
                expected.setdefault(section, {})[key] = value
            scalar.setdefault(section, {})[key] = value
        else:
            raise ValueError(f"Unknown configuration path: {path}")
    output, section, skipping = [], None, False

    def flush():
        for key, value in scalar.pop(section, {}).items():
            if value is not None:
                output.append(f"{key} = {literal(value)}\n")

    for kind, key, text in statements(source):
        if kind in ("table", "array"):
            flush()
            section = key
            skipping = kind == "array" and key in arrays
        if skipping:
            continue
        if kind == "value" and section is None and key in arrays:
            continue
        if kind == "value" and key in scalar.get(section, {}):
            value = scalar[section].pop(key)
            if value is not None:
                output.append(f"{key} = {literal(value)}{trailing_comment(text)}\n")
        else:
            output.append(text if text.endswith("\n") else text + "\n")
    flush()
    for section, fields in scalar.items():
        values = [f"{key} = {literal(value)}\n" for key, value in fields.items() if value is not None]
        if values:
            output.extend([f"\n[{section}]\n", *values])
    for section, entries in arrays.items():
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) - {"id", "argv", "timeout_seconds"}:
                raise ValueError(f"Invalid {section} command")
            output.append(f"\n[[{section}]]\n")
            output.extend(f"{key} = {literal(value)}\n" for key, value in entry.items())
        if not entries:
            # Empty arrays must live at top level, before any table header.
            output.insert(0, f"{section} = []\n")
    result = "".join(output)
    try:
        matches = tomllib.loads(result) == expected
    except tomllib.TOMLDecodeError:
        matches = False
    if not matches:
        raise ValueError("This TOML layout needs the source editor; original document is unchanged")
    return result
