import argparse
import signal
import sys

from .engine import Engine
from .errors import DBError
from .protocol import ModeChange, error, result, statements
from .sql import Parser


def main():
    parser = argparse.ArgumentParser(description='MiniVectorDB local development SQL adapter')
    parser.add_argument('--data-dir', default='/data')
    parser.add_argument('--mode', choices=('exact', 'ann'), default='exact')
    parser.add_argument('--sort-memory-mib', type=float, default=4)
    parser.add_argument('--cache-memory-mib', type=float, default=16)
    parser.add_argument('--checkpoint-mib', type=float, default=64)
    parser.add_argument('--no-ready', action='store_true')
    parser.add_argument('--disable-native', action='store_true')
    parser.add_argument('--wire', choices=('sql', 'jsonl'), default='sql')
    parser.add_argument('--local-mode-controls', action='store_true',
                        help='Enable local self-test --mode lines; NOT an official wire protocol')
    args = parser.parse_args()
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='strict')
    engine = None
    try:
        engine = Engine(args.data_dir, int(args.sort_memory_mib * 1024 * 1024), args.mode,
                        int(args.cache_memory_mib * 1024 * 1024), int(args.checkpoint_mib * 1024 * 1024),
                        not args.disable_native)
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        if args.wire == 'jsonl':
            from .json_protocol import serve
            closed = serve(engine, sys.stdin, sys.stdout)
            if closed:
                engine = None
            return 0
        if not args.no_ready:
            print('READY', flush=True)
        try:
            for sql in statements(sys.stdin, local_mode_controls=args.local_mode_controls):
                try:
                    if isinstance(sql, ModeChange):
                        engine.mode = sql.mode
                        result(sys.stdout, 0)
                        continue
                    parsed = Parser(sql)
                    if parsed.tokens:
                        result(sys.stdout, engine.execute_statement(parsed.parse()))
                except DBError as exc:
                    error(sys.stdout, exc)
        except DBError as exc:
            error(sys.stdout, exc)
        return 0
    except (OSError, RuntimeError) as exc:
        # Storage failure is fatal: never report a normal statement failure and
        # continue after a possibly durable commit.
        print(f'Fatal storage/runtime error: {exc}', file=sys.stderr, flush=True)
        return 1
    finally:
        if engine is not None:
            engine.close()


if __name__ == '__main__':
    raise SystemExit(main())
