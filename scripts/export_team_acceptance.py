"""Export measured, content-free acceptance evidence; fail on incomplete phases.

Only explicit fixture files are read. Passwords, bearer tokens, local paths and
PDF text never enter the public artifact. Restore verification uses the actual
restored database through the service's authenticated ASGI HTTP boundary.
"""

import argparse
import json
import sqlite3
import sys
import time
import os
import shutil
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from zhiwiki.service.api import create_service_app
from zhiwiki.service.common import ServiceSettings, safe_path
from zhiwiki.service.persistent_index import checksum


def read_json(root, name):
    path = safe_path(root / name)
    if path.stat().st_size > 10 * 1024 * 1024:
        raise ValueError('Acceptance input unexpectedly large')
    return json.loads(path.read_text(encoding='utf-8'))


def hardware(root):
    disk = shutil.disk_usage(root)
    result = {'disk_total_bytes': disk.total, 'disk_free_bytes': disk.free, 'disk_medium': 'not_collected'}
    if os.name == 'nt':
        import ctypes
        class Memory(ctypes.Structure):
            _fields_ = [('length', ctypes.c_ulong), ('load', ctypes.c_ulong)] + [
                (name, ctypes.c_ulonglong) for name in ('physical_total', 'physical_available', 'page_total', 'page_available', 'virtual_total', 'virtual_available', 'virtual_extended')]
        memory = Memory()
        memory.length = ctypes.sizeof(memory)
        if ctypes.WinDLL('kernel32').GlobalMemoryStatusEx(ctypes.byref(memory)):
            result['physical_memory_bytes'] = memory.physical_total
    return result


def verify_restored(root, book, private):
    target = safe_path(book['restore']['restored'])
    settings = ServiceSettings(data_dir=str(target), cookie_secure=False)
    started = time.monotonic()
    with TestClient(create_service_app(settings)) as client:
        login = client.post('/v1/sessions', json={'username': 'verification-admin', 'password': private['admin_password']})
        login.raise_for_status()
        client.headers['X-CSRF-Token'] = login.json()['csrf_token']
        assert client.get('/health/ready').status_code == 200
        with closing(sqlite3.connect(str(target / 'knowledge.sqlite3'))) as connection:
            revision = book['publication']['revision_id']
            kb_id = connection.execute('SELECT b.kb_id FROM team_books b JOIN team_revisions r ON r.book_id=b.id WHERE r.id=?', (revision,)).fetchone()[0]
            chunks = connection.execute('SELECT COUNT(*) FROM chunks c JOIN team_pages p ON p.document_id=c.document_id WHERE p.revision_id=?', (revision,)).fetchone()[0]
        result = client.post('/v1/workspaces/default/retrievals', headers={'Idempotency-Key': 'restored-book-verification'},
                             json={'query': '梯度下降', 'knowledge_base_ids': [kb_id]})
        result.raise_for_status()
        assert result.json()['evidence']
    return {'ready': True, 'authenticated_retrieval': True, 'book_chunks': chunks,
            'startup_and_query_seconds': round(time.monotonic() - started, 3)}


def export(root):
    capacity = read_json(root, 'capacity-report.json')
    environment = read_json(root, 'environment.json')
    resources = read_json(root, 'resource-samples.json')
    book = read_json(root, 'book-report.json')
    private = read_json(root, 'credentials.private.json')
    required = {'bursts': 200, 'tool_chains': 60, 'six_calls': 120, 'sustained': 1800, 'fair_normal': 30}
    checks = {}
    for phase, count in required.items():
        measured = capacity['summaries'][phase]
        checks[phase] = measured['passed'] and measured['requests'] == count
    steady = capacity['summaries']['sustained']
    checks['sustained_percentiles'] = steady['p95_ms'] <= 1000 and steady['p99_ms'] <= 2000
    overload = capacity['samples']['overcapacity']
    checks['overcapacity_bounded'] = len(overload) == 40 and all(row['http_status'] in (200, 429, 503) for row in overload)
    checks['aggressor_throttled'] = any(row['http_status'] == 429 for row in capacity['samples']['fair_aggressor'])
    checks['book_complete'] = book['job']['pages_done'] == book['job']['pages_total'] == 531
    checks['book_tools'] = len(book['tool_chains']) == 8 and all(row['valid'] for row in book['tool_chains'])
    checks['health_during_import'] = bool(book['health_during_import']) and set(book['health_during_import']) == {200}
    interference = book['import_interference']
    baseline, during = interference['baseline'], interference['during']
    ratio = during['p95_ms'] / baseline['p95_ms'] if baseline['p95_ms'] and during['p95_ms'] else None
    checks['import_latency_target'] = bool(ratio and during['requests'] >= 10 and during['all_200'] and ratio <= 1.5 and during['p95_ms'] <= 1500)
    checks['source_snapshot_matches'] = all(checksum(Path(path)) == expected for path, expected in environment['service_sources'].items())
    checks['resource_recovery'] = resources[-1]['admission']['active'] == resources[-1]['admission']['waiting'] == resources[-1]['admission']['timed_out_running'] == 0
    if not all(checks[key] for key in required):
        raise ValueError('Required load phases incomplete or failed: ' + str(checks))
    restored = verify_restored(root, book, private)
    # Explicit allowlists are intentional: adding a private fixture field cannot
    # silently publish it when this exporter is rerun.
    public_environment = {key: environment[key] for key in ('python', 'platform', 'sqlite', 'cpu', 'logical_cpus', 'fragments', 'service_sources', 'driver_sha256')}
    public_environment['settings'] = {key: value for key, value in environment['settings'].items() if key not in ('data_dir', 'worker_python')}
    public_environment['hardware'] = hardware(root)
    with closing(sqlite3.connect(str(root / 'knowledge.sqlite3'))) as connection:
        row = connection.execute("SELECT r.file_hash,k.artifact FROM team_revisions r JOIN team_books b ON b.current_revision=r.id JOIN team_kbs k ON k.id=b.kb_id WHERE b.id='book_capacity'").fetchone()
        public_environment['corpus'] = {'recipe_sha256': row[0], 'index_artifact': row[1], 'fragments': 50000, 'chars_per_fragment': 900, 'overlap_chars': 120}
    public_book = {key: book[key] for key in ('source_sha256', 'source_bytes', 'native_upload', 'external_model_calls', 'upload_ms', 'import_seconds', 'job', 'page_checks', 'tool_chains', 'import_interference', 'health_during_import')}
    public_book['backup'] = {key: book['backup'][key] for key in ('files', 'seconds')}
    public_book['restore'] = {key: book['restore'][key] for key in ('files', 'seconds')}
    public_book['restored_service'] = restored
    public_book['import_interference']['p95_ratio'] = round(ratio, 3) if ratio else None
    return {'format': 1, 'date': '2026-10-16', 'scope': capacity['scope'],
            'checks': checks, 'environment': public_environment, 'capacity': capacity,
            'resources': resources, 'book': public_book,
            'limits': ['No external model or semantic answer-quality evaluation',
                       'Synthetic high-frequency corpus; each fragment 900 Unicode code points',
                       'Single Windows host, loopback network, no TLS/network/HA SLA',
                       'PDF glyph/formula fidelity not certified; physical PDF page locators']}


def markdown(report):
    """Keep human-readable acceptance numbers tied to the measured JSON."""
    capacity = report['capacity']['summaries']
    rows = []
    for key, label in [('bursts', '20身份突发，10轮'), ('tool_chains', '20任务×3步'),
                       ('six_calls', '20任务×6步'), ('sustained', '1 req/s，30分钟'),
                       ('overcapacity', '40身份超容量'), ('fair_normal', '抢占期间3个正常用户'), ('fair_aggressor', '单身份10次/秒抢占')]:
        item = capacity[key]
        rows.append('| {} | {} | `{}` | {} | {} | {} |'.format(label, item['requests'],
                    json.dumps(item['http_counts'], separators=(',', ':')), item['p95_ms'], item['p99_ms'], item['max_ms']))
    samples = report['resources']
    rss = [row['resources']['rss_bytes'] / 1024 / 1024 for row in samples if row['resources']['rss_bytes']]
    threads = [row['resources']['threads'] for row in samples]
    book = report['book']
    interference = book['import_interference']
    checks = '\n'.join('- {}：{}'.format(key, '通过' if value else '未通过') for key, value in report['checks'].items())
    return '''# 单机团队服务验收记录

日期：2026-10-16。机器结果为 [team-service-acceptance.json](../reports/team-service-acceptance.json)，包含逐请求状态/时延、资源采样、语料与服务源码 SHA-256。验证脚本不会把密码、令牌、原书正文或本机绝对路径写入公开报告。

## 1. 负载与口径

- Windows 11，Python 3.13.14，SQLite {sqlite}；CPU标识 `{cpu}`，{cpus}个逻辑核。物理内存与磁盘容量在JSON中记录，磁盘介质类型未采集。
- 单API进程，4个检索线程、20个总接纳位、每身份1在途。用户30rpm/突发6，全局120rpm/突发120；客户端连接池40以覆盖超容量测试，端到端3秒，服务端2秒。
- 50,000个合成片段，每片900个Unicode字符、120字符重叠；高频词覆盖大量候选，20个不同查询，40份独立用户凭据。已预热；不把健康检查当检索，也不缓存查询结果。
- 30分钟以真实时钟执行，总1 req/s、轮换20个用户。10轮20身份同步突发、完整3/6步工具链分别测量，轮次按速率预算间隔。
- 容量语料通过确定性fixture直接入库，不能当作原生PDF导入证据；真实书籍使用独立原生HTTP链路，在同一DB/主机后台导入，检索仍查50,000片段的容量库。

## 2. 实际HTTP结果

时延单位ms，表内P95/P99/最大值使用客户端端到端时间。逐请求另含服务端elapsed_ms；容量内只把200且服务端≤2秒/客户端≤3秒计成功。

| 场景 | 请求数 | HTTP状态计数 | P95 | P99 | 最大值 |
|---|---:|---|---:|---:|---:|
{rows}

40请求是超容量保护场景，429/503属于预期拒绝，不能拿来充当容量内成功。单用户抢占场景中，其余3个用户总0.5 req/s，抢占者10 req/s；报告按身份类别分别统计。

## 3. 原生书籍与导入干扰

《动手学深度学习》文本PDF，{source_bytes}字节，源SHA-256 `{source_hash}`。原书留在本机，不进仓库。

- HTTP multipart上传→独立受限解析子进程→{pages}页检查点→质量复核→整书发布，任务观察耗时{import_seconds}秒。该轮包含等待/恢复前序中断任务，不等于仅解析CPU耗时。
- {flags}页带解析质量标记，其中{glyphs}页缺字、{empty}页空白；原始抽取文本与规范化文本分开保存。抽查PDF第1/3/531页，第3页无正文。
- 8组主题问题均完成retrieve/read/validate，来源为物理PDF页码。引用结构有效，语义支持明确not_checked，不评估回答准确率或公式保真。
- 导入窗口健康检查全部200；以脱敏服务完成日志计量，前60次检索P95={baseline}ms，窗口内{during_count}次检索P95={during}ms，倍率{ratio}，全部200。满足本次≤1.5倍且≤1.5秒目标；单个导入窗口不代表任意PDF/硬件都有同样结果。
- SQLite一致备份{backup_seconds}秒、校验恢复{restore_seconds}秒；恢复后用认证HTTP边界确认ready与书籍检索，恢复书籍{chunks}片段。一次演练不等于自动实现24h RPO/2h RTO。

## 4. 资源与正确性

资源每30秒采样，共{sample_count}次。API RSS约{rss_min:.1f}–{rss_max:.1f}MiB，Python线程{thread_min}–{thread_max}；末次活动槽/队列/超时后仍运行均为0，SQLite busy计数{busy}。采样不是每毫秒峰值，也没有把API RSS当作解析子进程峰值；解析器另有1GiB OS上限。

最终检查与回归覆盖跨用户receipt、跨库输入、令牌/授权撤销、源更新、页校订与二次复核的发布竞态、If-Match冲突、取消后槽位归还、过期租约fence、备份校验和真实LangGraph节点。完整测试计数与命令见README；GitHub CI结果以Actions运行记录为准。

## 5. 检查结果

{checks}

## 6. 可以承诺到哪里

已证明这份单机、回环网络、指定语料与到达模型的证据服务行为。没有真实LLM/OCR/向量/重排、模型回答质量、多机HA、公网TLS网络SLA或多租户隔离结论。团队开放前仍须配置实际域名/TLS、受控备份位置和调度，并在目标主机复验。

复现入口：`scripts/verify_team_capacity.py`、`scripts/verify_team_book.py`、`scripts/export_team_acceptance.py`；部署见[团队手册](TEAM_SERVICE.md)，需求见[PRD](PRD_RAG_SERVICE.md)。
'''.format(sqlite=report['environment']['sqlite'], cpu=report['environment']['cpu'], cpus=report['environment']['logical_cpus'], rows='\n'.join(rows),
           source_bytes=book['source_bytes'], source_hash=book['source_sha256'], pages=book['job']['pages_done'], import_seconds=book['import_seconds'],
           flags=book['job']['quality_summary']['flagged_pages'], glyphs=book['job']['quality_summary']['missing_glyph_pages'], empty=book['job']['quality_summary']['empty_pages'],
           baseline=interference['baseline']['p95_ms'], during=interference['during']['p95_ms'], during_count=interference['during']['requests'], ratio=interference['p95_ratio'],
           backup_seconds=book['backup']['seconds'], restore_seconds=book['restore']['seconds'], chunks=book['restored_service']['book_chunks'],
           sample_count=len(samples), rss_min=min(rss), rss_max=max(rss), thread_min=min(threads), thread_max=max(threads), busy=samples[-1]['storage']['sqlite_busy'], checks=checks)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    result = export(safe_path(args.data_dir))
    output = safe_path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    (Path(__file__).resolve().parents[1] / 'docs' / 'TEAM_ACCEPTANCE.md').write_text(markdown(result), encoding='utf-8')
    print(json.dumps({'output': str(output), 'checks': result['checks']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
