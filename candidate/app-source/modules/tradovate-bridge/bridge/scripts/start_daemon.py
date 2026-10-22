#!/usr/bin/env python3
"""Start the bridge daemon once its browser's DevTools port answers (the units' single ExecStart).

At boot the daemon can start before its Chromium window opens DevTools. It would exit and burn its
systemd start limit. This waits up to --wait seconds (default 90) for http://127.0.0.1:<devtools_port>/
json/version, with devtools_port read from the daemon config, then replaces itself with
`python3 -m tvbridge.daemon --config <config>`. Exit 1 when the port never answers.
"""
import argparse
import json
import os
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
BRIDGE = os.path.dirname(HERE)


def devtools_answers(port, timeout=1.0):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        opener.open('http://127.0.0.1:%d/json/version' % port, timeout=timeout).close()
        return True
    except Exception:
        return False


def wait_for(port, seconds, probe=devtools_answers, clock=time.monotonic, sleep=time.sleep):
    end = clock() + seconds
    while True:
        if probe(port):
            return True
        if clock() >= end:
            return False
        sleep(1.0)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('--config', required=True)
    ap.add_argument('--wait', type=float, default=90.0)
    args = ap.parse_args(argv)
    with open(args.config) as f:
        port = json.load(f).get('devtools_port')
    if type(port) is not int or not 0 < port < 65536:
        print('config devtools_port must be a port number', file=sys.stderr)
        return 1
    if not wait_for(port, args.wait):
        print('DevTools port %d did not answer within %d s' % (port, args.wait), file=sys.stderr)
        return 1
    os.chdir(BRIDGE)
    os.execv(sys.executable, [sys.executable, '-m', 'tvbridge.daemon', '--config', args.config])


if __name__ == '__main__':
    sys.exit(main())
