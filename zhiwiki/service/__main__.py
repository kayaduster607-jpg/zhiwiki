"""Explicit team CLI; secrets are prompted or read from named process variables."""

import argparse
import getpass
import json
import os

from .common import load_service_settings


def main():
    parser = argparse.ArgumentParser(description="知证团队检索服务；不读取.env")
    parser.add_argument("--config")
    actions = parser.add_subparsers(dest="action", required=True)
    bootstrap = actions.add_parser("bootstrap")
    bootstrap.add_argument("username")
    bootstrap.add_argument("--password-env")
    serve = actions.add_parser("serve")
    serve.add_argument("--port", type=int, default=8872)
    serve.add_argument("--local-http", action="store_true", help="仅回环开发时允许非Secure cookie")
    worker = actions.add_parser("worker")
    worker.add_argument("--once", action="store_true")
    job = actions.add_parser("process-job")
    job.add_argument("job_id")
    job.add_argument("fence", type=int)
    backup = actions.add_parser("backup")
    backup.add_argument("destination")
    restore = actions.add_parser("restore")
    restore.add_argument("snapshot")
    restore.add_argument("destination")
    actions.add_parser("cleanup")
    args = parser.parse_args()
    settings = load_service_settings(args.config)
    if args.action == "serve":
        import uvicorn
        from .api import create_service_app
        if args.local_http:
            settings.cookie_secure = False
        import logging
        logging.basicConfig(level=logging.INFO, format='%(message)s')
        uvicorn.run(create_service_app(settings), host="127.0.0.1", port=args.port, workers=1, limit_concurrency=128,
                    proxy_headers=True, forwarded_allow_ips='127.0.0.1')
    elif args.action == "bootstrap":
        from .auth import Authorization
        from .database import ServiceDatabase
        database = ServiceDatabase(settings)
        with database.tx() as connection:
            if connection.execute("SELECT 1 FROM team_users WHERE admin=1").fetchone():
                parser.error("An administrator already exists; use the authenticated admin API")
        password = os.environ.get(args.password_env, "") if args.password_env else getpass.getpass("管理员密码（至少12字符）: ")
        user_id = Authorization(database).create_user(args.username, password, admin=True)
        print(json.dumps({"administrator": args.username, "id": user_id}))
    elif args.action in ("worker", "process-job"):
        from .worker import process_job, supervise
        if args.action == "worker":
            supervise(settings, args.once)
        else:
            from .limits import limit_parser_memory
            limit_parser_memory(settings.worker_memory_mb)
            process_job(settings, args.job_id, args.fence)
    else:
        from .operations import backup_workspace, cleanup, restore_workspace
        if args.action == "backup":
            print(json.dumps(backup_workspace(settings, args.destination)))
        elif args.action == "restore":
            print(json.dumps(restore_workspace(args.snapshot, args.destination)))
        else:
            print(json.dumps(cleanup(settings)))


if __name__ == "__main__":
    main()
