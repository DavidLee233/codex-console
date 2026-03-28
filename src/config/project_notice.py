"""Shared project notice content for terminal and Web UI."""

PROJECT_NOTICE = {
    "title": "免责声明",
    "github_repo_name": "DavidLee233/codex-console",
    "github_repo_url": "https://github.com/DavidLee233/codex-console",
    "disclaimer": (
        "免责声明：本工具仅供学习和研究使用，使用本工具产生的一切后果由使用者自行承担。"
        "请遵守相关服务的使用条款，不要用于任何违法或不当用途。"
        "如有侵权，请及时联系，会及时删除。"
    ),
}


def build_terminal_notice_lines() -> list[str]:
    """Build terminal-friendly notice lines."""
    return [
        "=" * 72,
        PROJECT_NOTICE["title"],
        f"GitHub 仓库 {PROJECT_NOTICE['github_repo_name']}：{PROJECT_NOTICE['github_repo_url']}",
        PROJECT_NOTICE["disclaimer"],
        "=" * 72,
    ]
