"""Project notice content used by terminal and web pages."""

PROJECT_NOTICE = {
    "title": "免责声明",
    "free_notice": "",
    "disclaimer": (
        "免责声明：本工具仅供学习和研究使用，使用本工具产生的一切后果由使用者自行承担。"
        "请遵守相关服务条款，不要用于违法或不当用途。如有侵权，请及时联系，将第一时间处理。"
    ),
    "support_notice": (
        "项目维护不易，服务器与开发都需要持续投入。"
        "如果这个项目对你有帮助，欢迎在有条件的情况下赞助支持。"
    ),
    "github_repo_name": "DavidLee233/codex-console",
    "github_repo_url": "https://github.com/DavidLee233/codex-console",
}


def build_terminal_notice_lines() -> list[str]:
    """Build terminal-friendly notice lines."""
    return [
        "=" * 72,
        PROJECT_NOTICE["title"],
        PROJECT_NOTICE["disclaimer"],
        PROJECT_NOTICE["support_notice"],
        f"GitHub 仓库 {PROJECT_NOTICE['github_repo_name']}：{PROJECT_NOTICE['github_repo_url']}",
        "=" * 72,
    ]
