#!/usr/bin/env python3
"""
argus_context_poller.py — Poll argus /context/status and write state/argus_context.json.

Reads ARGUS_URL from state/agent_config.env. If argus is unreachable, writes a
reachable=false state file and exits 0 (non-fatal by design).

Usage:
    python3 tools/argus_context_poller.py

Output: state/argus_context.json
"""

import json
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

PROJECT_DIR = Path(__file__).parent.parent.parent
CONFIG_ENV = PROJECT_DIR / 'state' / 'agent_config.env'
OUTPUT_FILE = PROJECT_DIR / 'state' / 'argus_context.json'

TIMEOUT_SECONDS = 5


def read_argus_url() -> str | None:
    if not CONFIG_ENV.exists():
        return None
    for line in CONFIG_ENV.read_text().splitlines():
        line = line.strip()
        if line.startswith('ARGUS_URL=') and not line.startswith('#'):
            return line.split('=', 1)[1].strip()
    return None


def fetch_argus_status(url: str) -> dict:
    endpoint = url.rstrip('/') + '/context/status'
    fetched_at = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    try:
        req = urllib.request.Request(endpoint, headers={'Accept': 'application/json'})
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        # Real argus schema: current_state, current_confidence, last_input_ago_seconds, uptime_seconds
        # Design spec names (state, confidence, idle_seconds, focus_vector) used as fallbacks
        return {
            'fetched_at': fetched_at,
            'reachable': True,
            'state': data.get('current_state', data.get('state', 'unknown')),
            'confidence': data.get('current_confidence', data.get('confidence', 0.0)),
            'idle_seconds': data.get('last_input_ago_seconds', data.get('idle_seconds')),
            'focus_vector': data.get('focus_vector'),
            'raw_uptime': data.get('uptime_seconds', data.get('uptime')),
            'error': None,
        }
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return {
            'fetched_at': fetched_at,
            'reachable': False,
            'state': 'unknown',
            'confidence': 0.0,
            'idle_seconds': None,
            'focus_vector': None,
            'raw_uptime': None,
            'error': str(e),
        }
    except (json.JSONDecodeError, KeyError) as e:
        return {
            'fetched_at': fetched_at,
            'reachable': False,
            'state': 'unknown',
            'confidence': 0.0,
            'idle_seconds': None,
            'focus_vector': None,
            'raw_uptime': None,
            'error': f'Parse error: {e}',
        }


def main():
    argus_url = read_argus_url()
    if not argus_url:
        print('argus_context_poller: ARGUS_URL not set in agent_config.env — skipping',
              file=sys.stderr)
        sys.exit(0)

    result = fetch_argus_status(argus_url)
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')

    if result['reachable']:
        print(f"argus: state={result['state']} confidence={result['confidence']:.2f}")
    else:
        print(f"argus: unreachable — {result['error']}", file=sys.stderr)


if __name__ == '__main__':
    main()
