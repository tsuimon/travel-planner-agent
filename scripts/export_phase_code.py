"""Export phase-ordered complete source listings without including runtime data or secrets."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PHASES = {
    0: [
        "requirements.txt",
        "requirements-dev.txt",
        "pyproject.toml",
        "environment.yml",
        ".env.example",
        ".env.deepseek.example",
        ".gitignore",
        ".dockerignore",
        "src/__init__.py",
        "src/config.py",
        "src/errors.py",
        "src/logging_config.py",
        "README.md",
    ],
    1: ["src/data/*.py", "scripts/init_db.py"],
    2: ["src/tools/*.py", "scripts/check_connections.py"],
    3: ["src/domain.py", "src/search/*.py", "scripts/load_sample_data.py"],
    4: ["src/agent/*.py"],
    5: ["src/memory.py", "src/rag/*.py", "src/rag/documents/*.json", "scripts/build_knowledge_base.py"],
    6: [
        "src/risk.py",
        "src/api/*.py",
        "frontend/*.py",
        "scripts/start_mobile.py",
        "mobile/android/AndroidManifest.xml",
        "mobile/android/res/**/*.xml",
        "mobile/android/src/**/*.java",
        "mobile/android/build.py",
    ],
    7: [
        "tests/*.py",
        "mobile/android/tests/*.java",
        "scripts/__init__.py",
        "scripts/demo.py",
        "scripts/benchmark.py",
        "scripts/export_phase_code.py",
        "Dockerfile",
        "docker-compose.yml",
        ".github/workflows/ci.yml",
    ],
}


def main() -> None:
    blocks = [
        "# Phase 0–7 完整代码快照\n\n由工作区源码自动生成。阶段目标、验证与衔接见 phases.md；架构文档见 architecture.md。"
    ]
    for phase, patterns in PHASES.items():
        blocks.append(f"## Phase {phase}")
        for pattern in patterns:
            files = sorted(ROOT.glob(pattern))
            if not files:
                raise FileNotFoundError(pattern)
            for path in files:
                relative = path.relative_to(ROOT).as_posix()
                language = {".py": "python", ".json": "json", ".yml": "yaml", ".toml": "toml"}.get(
                    path.suffix, "text"
                )
                content = path.read_text(encoding="utf-8")
                fence = "`" * max(
                    4,
                    max((len(line) - len(line.lstrip("`")) for line in content.splitlines()), default=0) + 1,
                )
                blocks.append(f"### `{relative}`\n\n{fence}{language}\n{content.rstrip()}\n{fence}")
    output = ROOT / "docs/phase-code.md"
    output.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")
    print(output)


if __name__ == "__main__":
    main()
