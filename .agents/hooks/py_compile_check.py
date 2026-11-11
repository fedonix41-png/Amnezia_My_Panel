#!/usr/bin/env python3
"""Lifecycle Stop hook for Antigravity IDE: verifies syntax of modified Python files."""

import json
import os
import py_compile
import subprocess
import sys


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        print(json.dumps({"decision": "allow"}))
        return

    if payload.get("terminationReason") != "NO_TOOL_CALL":
        print(json.dumps({"decision": "allow"}))
        return

    repo_root = payload.get("cwd") or os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..")
    )

    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain", "-uall"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception:
        print(json.dumps({"decision": "allow"}))
        return

    errors: list[str] = []
    for line in proc.stdout.splitlines():
        if len(line) < 4:
            continue
        status = line[:2]
        if "D" in status:
            continue
        rel_path = line[3:].strip()
        if " -> " in rel_path:
            rel_path = rel_path.split(" -> ", 1)[1].strip()
        if rel_path.startswith('"') and rel_path.endswith('"'):
            rel_path = rel_path[1:-1]
        if not rel_path.endswith(".py"):
            continue
        if rel_path.startswith(("venv/", ".venv/", ".kilo/")):
            continue

        full_path = os.path.join(repo_root, rel_path)
        if not os.path.isfile(full_path):
            continue

        try:
            py_compile.compile(full_path, doraise=True)
        except py_compile.PyCompileError as exc:
            errors.append(f"{rel_path}: {exc.msg}")

    if errors:
        reason = (
            "Автопроверка py_compile обнаружила синтаксические ошибки в изменённых файлах:\n"
            + "\n".join(errors)
            + "\nИсправь синтаксис перед завершением."
        )
        print(json.dumps({"decision": "continue", "reason": reason}, ensure_ascii=False))
    else:
        print(json.dumps({"decision": "allow"}))


if __name__ == "__main__":
    main()

