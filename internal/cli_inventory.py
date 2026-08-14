"""Parse and audit the option declarations printed by llama-server --help."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable, Sequence


# llama.cpp aligns normal declarations to column 40.  Declarations which are
# longer than that are put on their own line, followed by an indented help line.
_HELP_COLUMN = 40
_SWITCH_RE = re.compile(
    r"(?<![\w.-])-{1,2}[A-Za-z](?:[A-Za-z0-9.-]*[A-Za-z0-9])?(?![\w.-])"
)


@dataclass(frozen=True, slots=True)
class CliOption:
    """One option declaration; aliases stay grouped as llama.cpp prints them."""

    switches: tuple[str, ...]
    declaration: str
    description: str
    section: str

    @property
    def canonical(self) -> str:
        long_switches = [item for item in self.switches if item.startswith("--")]
        return long_switches[-1] if long_switches else self.switches[-1]


def _declaration_prefix(line: str) -> str:
    if len(line) <= _HELP_COLUMN:
        return line
    first_column = line[:_HELP_COLUMN]
    # Padding at the end of the first column means the description starts at
    # column 40.  Without padding, llama.cpp wrapped a long declaration.
    return first_column if first_column[-1].isspace() else line


def parse_help_options(help_text: str) -> tuple[CliOption, ...]:
    """Return every real option line, excluding section headings and prose."""
    lines = help_text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    options: list[CliOption] = []
    section = ""
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if stripped.startswith("-----") and stripped.endswith("-----"):
            section = stripped.strip("- ")
            index += 1
            continue
        if not re.match(r"^-{1,2}[A-Za-z]", line):
            index += 1
            continue

        prefix = _declaration_prefix(line).rstrip()
        switches = tuple(match.group(0) for match in _SWITCH_RE.finditer(prefix))
        if not switches:
            index += 1
            continue

        if prefix == line.rstrip() and len(line) >= _HELP_COLUMN and not line[:_HELP_COLUMN].endswith(" "):
            description_parts: list[str] = []
        else:
            description_parts = [line[_HELP_COLUMN:].strip()] if len(line) > _HELP_COLUMN else []
        cursor = index + 1
        while cursor < len(lines):
            continuation = lines[cursor]
            if re.match(r"^-{1,2}[A-Za-z]", continuation) or continuation.strip().startswith("-----"):
                break
            if continuation.startswith(" " * _HELP_COLUMN):
                text = continuation[_HELP_COLUMN:].strip()
                if text:
                    description_parts.append(text)
            elif continuation.strip():
                break
            cursor += 1
        options.append(
            CliOption(
                switches=switches,
                declaration=prefix,
                description=" ".join(description_parts),
                section=section,
            )
        )
        index = cursor
    return tuple(options)


def option_switches(options: Iterable[CliOption]) -> frozenset[str]:
    return frozenset(switch for option in options for switch in option.switches)


# These options inspect state or print output and then terminate instead of
# configuring a server process.
ACTION_META_SWITCHES = frozenset(
    {
        "-h",
        "--help",
        "--usage",
        "--version",
        "-cl",
        "--cache-list",
        "--completion-bash",
        "--list-devices",
    }
)

# These declarations remain visible solely to produce an explicit migration
# error. llama.cpp's handlers reject them and point to the replacement options.
REMOVED_SWITCHES = frozenset(
    {
        "--draft",
        "--draft-n",
        "--draft-max",
        "--draft-min",
        "--draft-n-min",
        "--spec-ngram-size-n",
        "--spec-ngram-size-m",
        "--spec-ngram-min-hits",
    }
)

# -m comes from the selected model and --mcp-servers-json is produced by the
# launcher's Web Search/MCP integration.  Both still flow through build_command.
LAUNCHER_MANAGED_SWITCHES = frozenset(
    {"-m", "--model", "--mcp-servers-json"}
)


def uncovered_options(
    options: Sequence[CliOption],
    represented_switches: Iterable[str],
) -> tuple[CliOption, ...]:
    """Find declarations with no spec, managed mechanism, or explicit reason."""
    covered = (
        set(represented_switches)
        | set(ACTION_META_SWITCHES)
        | set(REMOVED_SWITCHES)
        | set(LAUNCHER_MANAGED_SWITCHES)
    )
    return tuple(option for option in options if not covered.intersection(option.switches))
