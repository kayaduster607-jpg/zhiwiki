"""Optional OS-level resource sampler for an explicitly identified team API.

Requires psutil in the observer's environment, not in the RAG server. Reads
process counters only: no environment variables, source text or open file names.
"""

import argparse
import json
import time
from pathlib import Path


def snapshot(process):
    memory = process.memory_info()
    io = process.io_counters()
    cpu = process.cpu_times()
    return {'at_unix': time.time(), 'rss_bytes': memory.rss, 'native_threads': process.num_threads(),
            'handles_or_fds': process.num_handles() if hasattr(process, 'num_handles') else process.num_fds(),
            'cpu_user_seconds': cpu.user, 'cpu_system_seconds': cpu.system,
            'read_bytes': io.read_bytes, 'write_bytes': io.write_bytes}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pid', type=int, required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--seconds', type=int, default=2400)
    args = parser.parse_args()
    import psutil
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from zhiwiki.service.common import safe_path
    process = psutil.Process(args.pid)
    command = process.cmdline()
    if 'zhiwiki.service' not in command or 'serve' not in command or not 1 <= args.seconds <= 7200:
        raise ValueError('Select the explicit team API process and a bounded duration')
    output = safe_path(args.output)
    report = {'psutil_version': psutil.__version__, 'interval_seconds': 15, 'samples': [], 'stop_reason': None}
    began = time.monotonic()
    while time.monotonic() - began < args.seconds:
        try:
            report['samples'].append(snapshot(process))
        except psutil.NoSuchProcess:
            report['stop_reason'] = 'service_exited'
            break
        output.write_text(json.dumps(report, indent=2), encoding='utf-8')
        time.sleep(15)
    report['stop_reason'] = report['stop_reason'] or 'observation_deadline'
    output.write_text(json.dumps(report, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
