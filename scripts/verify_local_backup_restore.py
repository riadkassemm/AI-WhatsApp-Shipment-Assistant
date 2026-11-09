"""Take a local backup and prove it restores, without touching production state.

Production MariaDB and Redis are only READ: a consistent ``--single-transaction``
dump and a Redis RDB snapshot (``redis-cli --rdb``). Both are restored into
throwaway, socket-only instances in a private work directory, verified, and
the instances are removed. The monitored receipt is written atomically ONLY
after every check passes; a failure never refreshes it.

Out of scope by design: WhatsApp authentication journals (never copied or
restored while their owner runs) and customer media (separate manifest checks).
Output and the receipt contain counts, sizes and hashes only.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time

IDENTIFIER = re.compile(r'^[A-Za-z0-9_]{1,64}$')
NAME = re.compile(r'^\d{8}T\d{6}Z$')


class VerificationFailed(RuntimeError):
    pass


def fail(code):
    raise VerificationFailed(code)


def sql(socket, statement, database=None):
    command = ['mariadb', '--no-defaults', '--socket=' + str(socket), '-uroot', '--batch', '--skip-column-names']
    if database:
        command.append(database)
    result = subprocess.run(command + ['-e', statement], capture_output=True, text=True, timeout=600)
    if result.returncode:
        fail('SQL_FAILED')
    return [row.split('\t') for row in result.stdout.splitlines()]


def inventory(socket, database):
    tables = {name: kind for name, kind in sql(socket, "SELECT TABLE_NAME, TABLE_TYPE FROM information_schema.TABLES "
                                                      f"WHERE TABLE_SCHEMA='{database}'")}
    if not tables or not all(IDENTIFIER.match(name) for name in tables):
        fail('SOURCE_INVENTORY_INVALID')
    counts = {}
    for name, kind in sorted(tables.items()):
        if kind == 'BASE TABLE':
            counts[name] = int(sql(socket, f'SELECT COUNT(*) FROM `{name}`', database)[0][0])
    routines = int(sql(socket, f"SELECT COUNT(*) FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA='{database}'")[0][0])
    triggers = int(sql(socket, f"SELECT COUNT(*) FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA='{database}'")[0][0])
    return {'tables': tables, 'counts': counts, 'routines': routines, 'triggers': triggers}


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def wait_for(check, code, seconds=120):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.5)
    fail(code)


@contextlib.contextmanager
def throwaway_mariadb(work):
    data, socket = work / 'mariadb', work / 'mariadb.sock'
    owner = ['--user=mysql'] if os.geteuid() == 0 else []
    if subprocess.run(['mariadb-install-db', '--no-defaults', '--datadir=' + str(data), '--auth-root-authentication-method=normal',
                       '--skip-test-db', *owner], capture_output=True, timeout=300).returncode:
        fail('RESTORE_INSTANCE_INIT_FAILED')
    process = subprocess.Popen(['mariadbd', '--no-defaults', '--skip-networking', '--datadir=' + str(data), '--socket=' + str(socket),
                                '--pid-file=' + str(work / 'mariadb.pid'), '--log-error=' + str(work / 'mariadb.err'),
                                '--innodb-buffer-pool-size=268435456', '--skip-log-bin', *owner],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_for(lambda: socket.exists() and subprocess.run(['mariadb-admin', '--no-defaults', '--socket=' + str(socket), '-uroot', 'ping'],
                 capture_output=True).returncode == 0, 'RESTORE_INSTANCE_START_FAILED')
        yield socket
    finally:
        process.terminate()
        try:
            process.wait(timeout=120)
        except subprocess.TimeoutExpired:
            process.kill(); process.wait()


@contextlib.contextmanager
def throwaway_redis(work, rdb):
    socket = work / 'redis.sock'
    process = subprocess.Popen(['redis-server', '--port', '0', '--unixsocket', str(socket), '--unixsocketperm', '700',
                                '--dir', str(work), '--dbfilename', rdb.name, '--save', '', '--appendonly', 'no'],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_for(lambda: socket.exists() and subprocess.run(['redis-cli', '-s', str(socket), 'ping'], capture_output=True,
                 text=True).stdout.strip() == 'PONG', 'RESTORE_REDIS_START_FAILED')
        yield socket
    finally:
        process.terminate()
        try:
            process.wait(timeout=60)
        except subprocess.TimeoutExpired:
            process.kill(); process.wait()


def verify(args):
    if not IDENTIFIER.match(args.database):
        fail('INVALID_DATABASE_NAME')
    args.artifacts.mkdir(mode=0o700, parents=True, exist_ok=True)
    if shutil.disk_usage(args.artifacts).free < args.min_free_bytes:
        fail('DISK_RESERVE_LOW')
    run = args.artifacts / time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
    run.mkdir(mode=0o700)
    try:
        return _verify_into(args, run)
    except BaseException:
        shutil.rmtree(run, ignore_errors=True)  # An unverified copy is not a backup.
        raise


def _verify_into(args, run):
    dump, rdb = run / 'database.sql.gz', run / 'redis.rdb'

    before = inventory(args.source_socket, args.database)
    with dump.open('wb') as raw, gzip.GzipFile(fileobj=raw, mode='wb', compresslevel=1) as compressed:
        process = subprocess.Popen(['mariadb-dump', '--no-defaults', '--socket=' + str(args.source_socket), '-uroot',
                                    '--single-transaction', '--skip-lock-tables', '--quick', '--routines', '--events',
                                    '--triggers', '--hex-blob', '--databases', args.database],
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        for chunk in iter(lambda: process.stdout.read(1024 * 1024), b''):
            compressed.write(chunk)
        if process.wait():
            fail('DATABASE_DUMP_FAILED')
    after = inventory(args.source_socket, args.database)
    if before['tables'] != after['tables']:
        fail('SOURCE_SCHEMA_CHANGED_DURING_DUMP')
    if subprocess.run(['redis-cli', '-s', str(args.redis_socket), '--rdb', str(rdb)], capture_output=True, timeout=600).returncode:
        fail('REDIS_SNAPSHOT_FAILED')
    if subprocess.run(['redis-check-rdb', str(rdb)], capture_output=True, timeout=600).returncode:
        fail('REDIS_SNAPSHOT_INVALID')

    with tempfile.TemporaryDirectory(prefix='restore-verify-', dir=args.work_base) as temporary:
        work = Path(temporary)
        work.chmod(0o700)
        if os.geteuid() == 0:
            shutil.chown(work, 'mysql', 'mysql')
        with throwaway_mariadb(work) as socket:
            # Stream decompressed SQL; a gzip object's fileno() is the compressed file.
            restore = subprocess.Popen(['mariadb', '--no-defaults', '--socket=' + str(socket), '-uroot'],
                                       stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                with gzip.open(dump, 'rb') as source:
                    for chunk in iter(lambda: source.read(1024 * 1024), b''):
                        restore.stdin.write(chunk)
            except BrokenPipeError:
                pass
            finally:
                restore.stdin.close()
            if restore.wait(timeout=3600):
                fail('DATABASE_RESTORE_FAILED')
            restored = inventory(socket, args.database)
            if restored['tables'] != before['tables'] or restored['routines'] != before['routines'] \
                    or restored['triggers'] != before['triggers']:
                fail('RESTORED_SCHEMA_MISMATCH')
            for name, count in restored['counts'].items():
                low, high = sorted((before['counts'][name], after['counts'][name]))
                if not low <= count <= high:
                    fail('RESTORED_ROW_COUNT_OUT_OF_BOUNDS')
            for name in restored['counts']:
                if sql(socket, f'CHECK TABLE `{name}`', args.database)[-1][-1] != 'OK':
                    fail('RESTORED_TABLE_CHECK_FAILED')
        restored_rdb = work / 'restore.rdb'
        shutil.copyfile(rdb, restored_rdb)
        with throwaway_redis(work, restored_rdb) as socket:
            keys = int(subprocess.run(['redis-cli', '-s', str(socket), 'dbsize'], capture_output=True, text=True).stdout.strip())

    receipt = {'verified': True, 'restored_at': time.time(), 'method': 'isolated_logical_restore',
               'database_tables': sum(kind == 'BASE TABLE' for kind in before['tables'].values()),
               'database_rows': sum(restored['counts'].values()), 'redis_keys': keys,
               'database_dump_sha256': digest(dump), 'redis_rdb_sha256': digest(rdb),
               'artifacts': str(run), 'off_server': False}
    (run / 'receipt.json').write_text(json.dumps(receipt))
    temporary_receipt = args.receipt.with_name('.' + args.receipt.name + '.pending')
    with temporary_receipt.open('w') as handle:
        os.fchmod(handle.fileno(), 0o600)
        handle.write(json.dumps(receipt))
        handle.flush(); os.fsync(handle.fileno())
    os.replace(temporary_receipt, args.receipt)
    # Retention: only this tool's own verified run directories.
    runs = sorted(path for path in args.artifacts.iterdir() if path.is_dir() and NAME.match(path.name) and (path / 'receipt.json').exists())
    for old in runs[:-args.keep]:
        shutil.rmtree(old)
    return {key: receipt[key] for key in ('verified', 'restored_at', 'database_tables', 'database_rows', 'redis_keys')}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', default='whatsapp_support')
    parser.add_argument('--source-socket', type=Path, default=Path('/run/mysqld/mysqld.sock'))
    parser.add_argument('--redis-socket', type=Path, default=Path('/run/redis/redis-server.sock'))
    parser.add_argument('--artifacts', type=Path, default=Path('/var/backups/whatsapp-support/verified'))
    parser.add_argument('--work-base', type=Path, default=Path('/var/tmp'))
    parser.add_argument('--receipt', type=Path, default=Path('/srv/migration/reports/verified-local-backup.json'))
    parser.add_argument('--keep', type=int, default=3)
    parser.add_argument('--min-free-bytes', type=int, default=21474836480)
    args = parser.parse_args()
    if args.keep < 1:
        parser.error('--keep must be at least 1')
    args.artifacts.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (args.artifacts / '.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            print(json.dumps(verify(args)), flush=True)
        except VerificationFailed as error:
            print(json.dumps({'verified': False, 'code': str(error)}), flush=True)
            raise SystemExit(1)


if __name__ == '__main__':
    main()
