"""可脚本化 CLI：每个子命令对应独立的小函数。"""

import argparse
import json
import sys
from pathlib import Path

from .bootstrap import seed_workspace
from .compiler import WikiCompiler
from .config import load_settings, reject_environment_path
from .engine import AgentEngine, BudgetExceeded
from .ingest import read_file
from .provider import ProviderError
from .store import KnowledgeStore


def build_parser():
    parser = argparse.ArgumentParser(description="知证 · Agentic RAG + Wiki")
    parser.add_argument("--config", help="显式 JSON 配置路径；不读取 .env")
    parser.add_argument("--data-dir", help="覆盖数据库目录")
    parser.add_argument("--provider", choices=["offline", "openai"], help="覆盖运行模式")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="只读检查现有解释器与依赖，不建库、不安装")
    commands.add_parser("init", help="创建本地数据库")
    commands.add_parser("seed", help="显式载入虚构原文与人工示例 Wiki")
    documents = commands.add_parser("documents", help="列出原始资料")
    documents.add_argument("--history", action="store_true")
    ingest = commands.add_parser("ingest", help="导入一个指定 MD/TXT/DOCX 文件")
    ingest.add_argument("file")
    ingest.add_argument("--source-key")
    ingest.add_argument("--title")
    query = commands.add_parser("ask", help="检索和回答问题")
    query.add_argument("question")
    query.add_argument("--strategy", choices=["baseline", "agentic"], default="agentic")
    query.add_argument("--trace", action="store_true", help="将公开步骤写到标准错误")
    serve = commands.add_parser("serve", help="启动本地操作台和 API")
    serve.add_argument("--host", choices=["127.0.0.1", "localhost"], default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    wiki = commands.add_parser("wiki", help="Wiki 草稿、发布、检查与导出")
    actions = wiki.add_subparsers(dest="wiki_action", required=True)
    listing = actions.add_parser("list")
    listing.add_argument("--history", action="store_true")
    draft = actions.add_parser("draft")
    draft.add_argument("slug")
    draft.add_argument("title")
    draft.add_argument("document_ids", nargs="+")
    for action in ("publish", "show", "export"):
        command = actions.add_parser(action)
        command.add_argument("id")
        if action == "export":
            command.add_argument("--output", help="可选导出路径；默认打印 Markdown")
    actions.add_parser("lint")
    evaluation = commands.add_parser("evaluate", help="隔离样例语料的来源命中回归；不等于答案正确率")
    evaluation.add_argument("--output", default="reports/local-evaluation.json")
    return parser


def print_json(value, stream=None):
    print(json.dumps(value, ensure_ascii=False, indent=2), file=stream or sys.stdout)


def write_artifact(path, content):
    # CLI 只写用户明确提供的导出路径，敏感文件命名规则仍然适用。
    destination = Path(path)
    reject_environment_path(destination)
    destination = destination.resolve()
    reject_environment_path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(content, encoding="utf-8")


def handle_wiki(args, store, settings):
    if args.wiki_action == "draft":
        return WikiCompiler(store, settings).draft(args.slug, args.title, args.document_ids)
    if args.wiki_action == "list":
        return store.list_wiki(include_history=args.history)
    if args.wiki_action == "publish":
        return store.publish_wiki(args.id)
    if args.wiki_action == "show":
        return store.get_wiki(args.id)
    if args.wiki_action == "lint":
        return store.lint_wiki()
    content = store.export_wiki(args.id)
    if args.output:
        write_artifact(args.output, content)
        return {"output": str(Path(args.output).resolve())}
    print(content)
    return None


def execute(args, settings):
    if args.command == "doctor":
        from .diagnostics import environment_report
        return environment_report()
    store = KnowledgeStore(settings.data_dir)
    if args.command == "init":
        return {"status": "ready", "data_dir": settings.data_dir, "mode": settings.provider}
    if args.command == "seed":
        return seed_workspace(store)
    if args.command == "documents":
        return store.list_documents(include_history=args.history)
    if args.command == "ingest":
        payload = read_file(args.file)
        for name in ("title", "source_key"):
            if getattr(args, name):
                payload[name] = getattr(args, name)
        return store.ingest(**payload)
    if args.command == "ask":
        callback = (lambda event: print_json(event, sys.stderr)) if args.trace else None
        return AgentEngine(store, settings).run(args.question, strategy=args.strategy, on_event=callback)
    if args.command == "wiki":
        return handle_wiki(args, store, settings)
    if args.command == "evaluate":
        from .evaluation import evaluate
        report = evaluate(settings)
        write_artifact(args.output, json.dumps(report, ensure_ascii=False, indent=2))
        return report
    if not 1 <= args.port <= 65535:
        raise ValueError("端口必须在 1–65535 之间")
    # 延迟导入：3.9 标准库环境仍可执行存储、离线查询与 Wiki 命令。
    import uvicorn
    from .api import create_app
    uvicorn.run(create_app(settings, store), host=args.host, port=args.port, log_level="info")
    return None


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        overrides = {name: getattr(args, name) for name in ("data_dir", "provider") if getattr(args, name) is not None}
        settings = load_settings(args.config, **overrides)
        result = execute(args, settings)
        if result is not None:
            print_json(result)
        if isinstance(result, dict) and result.get("status") in ("provider_error", "budget_exceeded", "invalid_answer"):
            return 2
        return 0
    except ProviderError:
        print("模型服务不可用，请检查显式 JSON 配置和运行时密钥。", file=sys.stderr)
    except BudgetExceeded:
        print("生成达到预算上限，请减少所选资料或调整预算。", file=sys.stderr)
    except (ValueError, KeyError, OSError) as error:
        print("操作失败：{}".format(error), file=sys.stderr)
    return 2
