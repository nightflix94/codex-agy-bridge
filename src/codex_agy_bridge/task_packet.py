"""Prompt packet formatting for bridge-owned task runs."""

from __future__ import annotations


def format_task_packet(
    prompt: str,
    *,
    completion_marker: str,
    workspace: str,
    artifact_dir: str | None = None,
    expected_file: str | None = None,
) -> str:
    """Return the lean task envelope sent to Antigravity task sessions."""
    task = prompt.rstrip()
    lines = [
        "Task:",
        task,
        "",
        "Acceptance:",
        "- Complete the requested work.",
        "",
        "Constraints:",
        "- Follow the user's instructions and the workspace conventions.",
        f"- Primary workspace directory: {workspace}",
        (
            "- Run project commands in that workspace and resolve relative project "
            "paths against it."
        ),
        (
            "- Write requested project files inside that workspace, not in "
            "Antigravity's global scratch directory."
        ),
    ]
    if artifact_dir:
        lines.extend(
            [
                f"- Write reports or handoff files under: {artifact_dir}",
                (
                    "- If the task asks for files, verify they exist and are "
                    "non-empty before finishing."
                ),
            ]
        )
    if expected_file:
        lines.extend(
            [
                f"- Required output file: {expected_file}",
                "- Finish only after that exact file exists and is non-empty.",
            ]
        )
    lines.extend(
        [
            "",
            "Expected output:",
            (
                "- A full and final response to the calling harness with "
                "changed files, "
                "verification, and any important caveats."
            ),
        ]
    )
    if artifact_dir:
        lines.append("- Mention any files written under the shared artifact directory.")
    head, tail = split_completion_marker(completion_marker)
    lines.extend(
        [
            "",
            "Completion marker:",
            (
                "Strongly suggested: first write the full and final response "
                "the calling harness should show the user. Then, only after all "
                "requested files and edits are complete, print the completion "
                "marker as the last line. Build the marker by joining the two "
                "parts below with nothing between them (no space, quotes, or "
                "labels):"
            ),
            f"Part 1: {head}",
            f"Part 2: {tail}",
        ]
    )
    return "\n".join(lines)


def split_completion_marker(marker: str) -> tuple[str, str]:
    """Split a marker so the packet never contains it verbatim.

    Antigravity's TUI echoes the whole packet into the terminal. If the packet
    held the literal marker, the echo would be indistinguishable from the agent
    printing it, and a Run could "complete" on its own prompt.
    """
    cut = marker.rfind("_") + 1
    if not 0 < cut < len(marker):
        cut = max(1, len(marker) // 2)
    return marker[:cut], marker[cut:]
