"""中文命令行入口。"""
import argparse
from .i18n import Parser, configure, t, write_summary
from pathlib import Path
import sys

from . import __version__
from .core import Limits, atomic_json, csv_jobs, run
from .har import TaskError, load_har, public_choice, strict_json, template_for


def main(argv=None):
    argv = configure(argv)
    parser = Parser(description="把网页查询变成参数表驱动的批量读取")
    parser.add_argument("--lang", choices=["zh", "en"], default="zh", help="Display language: zh or en")
    parser.add_argument("--version", action="version", version=__version__)
    actions = parser.add_subparsers(dest="action", required=True)
    inspect = actions.add_parser("inspect", help="查看浏览记录中的可用读取来源")
    inspect.add_argument("har", type=Path)
    prepare = actions.add_parser("prepare", help="保存本机读取配置")
    prepare.add_argument("har", type=Path)
    prepare.add_argument("--entry", type=int, required=True, help="浏览记录里的来源序号")
    prepare.add_argument("--out", type=Path, required=True)
    prepare.add_argument("--records-pointer", default=None)
    prepare.add_argument("--total-pointer", default="")
    prepare.add_argument("--page-param", default="")
    prepare.add_argument("--id-field", default="")
    batch = actions.add_parser("run", help="批量运行或续跑")
    batch.add_argument("template", type=Path)
    batch.add_argument("jobs", type=Path)
    batch.add_argument("--out", type=Path, required=True)
    batch.add_argument("--resume", action="store_true")
    batch.add_argument("--start-page", type=int, default=1)
    batch.add_argument("--max-pages", type=int, default=100)
    batch.add_argument("--max-records", type=int, default=100000)
    batch.add_argument("--timeout", type=float, default=15)
    batch.add_argument("--max-seconds", type=float, default=600)
    batch.add_argument("--retries", type=int, default=2)
    batch.add_argument("--interval", type=float, default=0.2)
    demonstration = actions.add_parser("demo", help="运行本机模拟完整任务")
    demonstration.add_argument("--out", type=Path, required=True)
    serve = actions.add_parser("serve", help="打开本机操作页面")
    serve.add_argument("--port", type=int, default=0)
    serve.add_argument("--workdir", type=Path, default=Path("运行结果"))
    serve.add_argument("--open", action="store_true", dest="open_browser")
    args = parser.parse_args(argv)
    try:
        if args.action == "inspect":
            import json
            print(json.dumps([public_choice(x) for x in load_har(args.har)], ensure_ascii=False, indent=2))
        elif args.action == "prepare":
            choices = load_har(args.har)
            choice = next((x for x in choices if x["entry"] == args.entry), None)
            if choice is None:
                raise TaskError("指定来源不存在或不支持读取")
            if args.out.exists():
                raise TaskError("配置文件已存在，不能覆盖")
            template = template_for(choice, records_pointer=args.records_pointer, total_pointer=args.total_pointer,
                                    page_param=args.page_param, id_field=args.id_field)
            args.out.parent.mkdir(parents=True, exist_ok=True)
            with args.out.open("x", encoding="utf-8") as stream:
                import json
                stream.write(json.dumps(template, ensure_ascii=False, indent=2) + "\n")
            print(t("读取配置已保存；可能含登录信息，请只保存在本机。"))
        elif args.action == "run":
            template = strict_json(args.template.read_text(encoding="utf-8-sig"))
            jobs = csv_jobs(args.jobs.read_text(encoding="utf-8-sig"))
            limits = Limits(**{name: getattr(args, name) for name in ["start_page", "max_pages", "max_records", "timeout", "max_seconds", "retries", "interval"]})
            result = run(template, jobs, args.out, limits=limits, resume=args.resume)
            print(t((args.out / "结论.txt").read_text(encoding="utf-8")))
            return 0 if result["status"] == "完成" else 2
        elif args.action == "demo":
            from .demo import demo
            demo(args.out)
            print(t((args.out / "批量结果/结论.txt").read_text(encoding="utf-8")))
        else:
            from .server import serve
            serve(args.port, args.workdir, args.open_browser)
        return 0
    except (TaskError, OSError) as error:
        print(t("本轮未完成：" + str(error)), file=sys.stderr)
        return 2
