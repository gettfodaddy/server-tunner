#!/usr/bin/env python3
"""Interactive, opt-in node tuning for Debian/Ubuntu Linux."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid

STATE = Path('/var/lib/prism-node-tuning')
CURRENT = STATE / 'current.json'
CONF = Path('/etc/sysctl.d/90-prism-node-tuning.conf')
SWAP = STATE / 'swapfile'
FSTAB = Path('/etc/fstab')
MARK = '# prism-node-tuning'


def styled(value, color='cyan', bold=False):
    if not sys.stdout.isatty() or os.environ.get('TERM') == 'dumb' or 'NO_COLOR' in os.environ:
        return str(value)
    colors = {'cyan': '36', 'green': '32', 'yellow': '33', 'red': '31', 'muted': '90'}
    return f'\033[{"1;" if bold else ""}{colors[color]}m{value}\033[0m'


def heading(title):
    print('\n' + styled('  ──  ' + title + '  ' + '─' * 30, 'cyan', True))


def notice(message, kind='info'):
    icon, color = {'info': ('i', 'cyan'), 'ok': ('✓', 'green'),
                   'warn': ('!', 'yellow'), 'error': ('×', 'red')}[kind]
    print(f'  {styled("[" + icon + "]", color, True)} {message}')


def prompt(message):
    return input(styled('  › ', 'cyan', True) + message).strip()


def confirm(message):
    return prompt(message + ' [да/нет]: ').casefold() in ('д', 'да', 'y', 'yes')


def interactive_visuals():
    return sys.stdout.isatty() and os.environ.get('TERM') != 'dumb'


def clear_screen():
    if interactive_visuals() and not os.environ.get('NO_CLEAR'):
        print('\033[2J\033[H', end='', flush=True)


def animate(message):
    if not interactive_visuals() or os.environ.get('NO_ANIMATION'):
        return
    for frame in ('◐', '◓', '◑', '◒', '◐', '◓'):
        print('\r' + styled('  ' + frame + '  ' + message, 'cyan'), end='', flush=True)
        time.sleep(0.10)
    print('\r\033[K', end='', flush=True)


def banner():
    print(styled('\n  NODE TUNING  /  SERVER SETUP', 'cyan', True))
    animate('Подготовка интерфейса')


def run(*args, check=True):
    try:
        p = subprocess.run([str(a) for a in args], text=True, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=120)
    except FileNotFoundError:
        raise RuntimeError(f'Команда не найдена: {args[0]}')
    if check and p.returncode:
        raise RuntimeError(f'{args[0]}: {(p.stderr or p.stdout).strip() or "ошибка"}')
    return p


def read(command, *args):
    if not shutil.which(command):
        return 'недоступно'
    p = run(command, *args, check=False)
    return p.stdout.strip() if p.returncode == 0 else 'недоступно'


def atomic_write(path, data, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=str(path.parent), prefix='.node-tuning-')
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def save(record):
    records = current()
    records[record['kind']] = record
    atomic_write(CURRENT, json.dumps(records, ensure_ascii=False, indent=2) + '\n')


def current():
    return json.loads(CURRENT.read_text(encoding='utf-8')) if CURRENT.exists() else {}


def begin(kind, details):
    if kind in current():
        raise RuntimeError('Это изменение уже активно. Сначала выполните его откат.')
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(STATE, 0o700)
    record = {'kind': kind, 'status': 'prepared', **details}
    save(record)
    return record


def meminfo():
    values = {}
    for line in Path('/proc/meminfo').read_text().splitlines():
        key, _, rest = line.partition(':')
        if rest.strip():
            values[key] = int(rest.split()[0]) * 1024
    return values


def default_iface():
    p = run('ip', '-j', 'route', 'show', 'default', check=False)
    if p.returncode:
        return ''
    routes = json.loads(p.stdout)
    return routes[0].get('dev', '') if len(routes) == 1 else ''


def system_info():
    heading('ИНФОРМАЦИЯ О СЕРВЕРЕ')
    mem = meminfo()
    print('  Узел        ', socket.gethostname())
    print('  Ядро        ', read('uname', '-r'))
    print('  Интерфейс   ', default_iface() or 'не определён однозначно')
    print('  RAM         ', f'{mem.get("MemTotal", 0) // (1024**2)} МБ')
    print('  Swap        ', f'{mem.get("SwapTotal", 0) // (1024**2)} МБ')
    print('  Изменения   ', ', '.join(current()) or 'нет')


def diagnostic():
    heading('ПРОВЕРКА СЕРВЕРА')
    animate('Собираем данные о сервере')
    commands = [
        ('Адреса', 'ip', '-br', 'address'),
        ('Маршрут', 'ip', 'route', 'show', 'default'),
        ('TCP и память', 'sysctl', 'net.ipv4.tcp_congestion_control',
         'net.ipv4.tcp_available_congestion_control', 'net.ipv4.tcp_rmem',
         'net.ipv4.tcp_wmem', 'net.ipv4.tcp_mtu_probing', 'vm.swappiness'),
        ('Conntrack', 'sysctl', 'net.netfilter.nf_conntrack_count', 'net.netfilter.nf_conntrack_max'),
        ('Очереди', 'tc', '-s', 'qdisc', 'show'),
        ('Память', 'free', '-h'),
        ('Swap', 'swapon', '--show'),
        ('Диск /var', 'df', '-h', '/var'),
        ('Нагрузка', 'uptime'),
        ('Лимиты Docker', 'systemctl', 'show', 'docker', '-p', 'LimitNOFILE', '-p', 'LimitNPROC'),
        ('Логирование Docker', 'docker', 'info', '--format', '{{.LoggingDriver}}'),
        ('Контейнеры', 'docker', 'ps', '--format', '{{.Names}}\t{{.Status}}'),
    ]
    details = [(name, read(cmd, *args)) for name, cmd, *args in commands]
    data = dict(details)
    def section(name):
        print('\n  ' + styled(name, 'cyan', True))
    def row(label, value):
        print(f'    {label:<18} {value or "нет данных"}')
    def kernel_value(key):
        result = read('sysctl', '-n', key)
        return ' '.join(result.split()) if result else 'нет данных'
    def human_bytes(value):
        return f'{value / 2**30:.2f} ГиБ' if value >= 2**30 else f'{value / 2**20:.0f} МиБ'

    iface = default_iface()
    addresses = []
    ip_data = read('ip', '-j', 'address', 'show', 'dev', iface) if iface else ''
    try:
        for interface in json.loads(ip_data):
            for addr in interface.get('addr_info', []):
                if addr.get('scope') == 'global':
                    addresses.append(f'{addr.get("local")}/{addr.get("prefixlen")}')
    except (ValueError, TypeError):
        pass
    section('СЕТЬ')
    row('Интерфейс', iface or 'не определён однозначно')
    row('Адреса', ', '.join(addresses) or 'нет глобальных адресов / недоступно')
    congestion = kernel_value('net.ipv4.tcp_congestion_control')
    row('TCP алгоритм', congestion)
    qdisc_text = data['Очереди']
    qdisc_lines = qdisc_text.splitlines()
    queue = 'недоступно'
    for index, line in enumerate(qdisc_lines):
        if iface and re.search(r'\bdev\s+' + re.escape(iface) + r'\s+root\b', line):
            kind = re.search(r'^qdisc\s+(\S+)', line)
            following = qdisc_lines[index + 1] if index + 1 < len(qdisc_lines) else ''
            drops = re.search(r'\bdropped\s+(\d+)', following)
            queue = (kind.group(1) if kind else 'неизвестно') + (f' · потери в очереди: {drops.group(1)}' if drops else '')
            break
    row('Очередь', queue)
    row('MTU probing', kernel_value('net.ipv4.tcp_mtu_probing'))
    count = kernel_value('net.netfilter.nf_conntrack_count')
    maximum = kernel_value('net.netfilter.nf_conntrack_max')
    percent = f' ({int(count) / int(maximum) * 100:.1f}%)' if count.isdigit() and maximum.isdigit() and int(maximum) else ''
    row('Conntrack', f'{count} / {maximum}{percent}')

    mem = meminfo()
    total = mem.get('MemTotal', 0); available = mem.get('MemAvailable', 0)
    swap_total = mem.get('SwapTotal', 0); swap_used = max(0, swap_total - mem.get('SwapFree', 0))
    disk = shutil.disk_usage('/var')
    section('РЕСУРСЫ')
    row('RAM', f'{human_bytes(available)} доступно из {human_bytes(total)}')
    row('Swap', f'{human_bytes(swap_used)} занято из {human_bytes(swap_total)}')
    row('Диск /var', f'{human_bytes(disk.free)} свободно из {human_bytes(disk.total)} ({disk.used / disk.total * 100:.0f}% занято)')
    try:
        row('Нагрузка 1/5/15', ' / '.join(f'{x:.2f}' for x in os.getloadavg()))
    except OSError:
        row('Нагрузка 1/5/15', 'недоступно')

    section('DOCKER')
    containers = data['Контейнеры']
    if containers == 'недоступно':
        row('Контейнеры', 'недоступно')
    elif not containers:
        row('Контейнеры', 'нет запущенных')
    else:
        for line in containers.splitlines():
            name, _, status = line.partition('\t')
            row(name, status)
    row('Драйвер логов', data['Логирование Docker'])
    print()
    notice('Без изменений настроек и нагрузочного теста.')
    if prompt('Подробнее: [д] полная диагностика / Enter — назад: ').casefold() in ('д', 'да', 'd', 'details'):
        heading('ПОЛНАЯ ДИАГНОСТИКА')
        for name, value in details:
            print('\n  ' + styled(name, 'cyan', True))
            print('\n'.join('    ' + line for line in (value or 'нет данных').splitlines()))


def sysctl_get(key):
    return run('sysctl', '-n', key).stdout.strip()


def tune_bbr():
    heading('BBR')
    if 'bbr' in current():
        raise RuntimeError('BBR уже настроен этим инструментом.')
    if CONF.exists() or CONF.is_symlink():
        raise RuntimeError(f'Файл {CONF} уже существует; проверьте его вручную.')
    available = sysctl_get('net.ipv4.tcp_available_congestion_control').split()
    if 'bbr' not in available:
        raise RuntimeError('BBR сейчас недоступен. Проверьте поддержку и загрузку модуля отдельно.')
    key = 'net.ipv4.tcp_congestion_control'
    before = sysctl_get(key)
    if before == 'bbr':
        notice('BBR уже выбран для новых TCP-соединений.', 'ok')
        return
    print(f'  {key}: {before} → bbr')
    print('  Изменение сохраняется в /etc/sysctl.d; существующая очередь не меняется.')
    if not confirm('Применить?'):
        return
    record = begin('bbr', {'previous': before, 'key': key})
    content = '# Управляется prism-node-tuning\nnet.ipv4.tcp_congestion_control = bbr\n'
    record['sha256'] = hashlib.sha256(content.encode()).hexdigest()
    save(record)
    atomic_write(CONF, content, 0o644)
    run('sysctl', '-w', key + '=bbr')
    if sysctl_get(key) != 'bbr':
        raise RuntimeError('Не удалось подтвердить BBR; используйте откат.')
    record['status'] = 'active'; save(record)
    notice('BBR включён для новых TCP-соединений.', 'ok')
    notice('Другие sysctl.d могут изменить значение при загрузке; проверьте после перезагрузки.', 'warn')


def parse_root_qdisc(iface):
    text = run('tc', 'qdisc', 'show', 'dev', iface).stdout.strip().splitlines()
    if len(text) != 1:
        raise RuntimeError('Обнаружено несколько очередей/классов. Автоматическая замена небезопасна.')
    parts = text[0].split()
    if len(parts) < 4 or parts[0] != 'qdisc' or 'root' not in parts:
        raise RuntimeError('Не удалось распознать корневую очередь.')
    kind = parts[1]
    if kind not in ('fq', 'fq_codel'):
        raise RuntimeError('Автоматическая замена поддерживает только одиночные fq и fq_codel; текущая очередь: ' + kind)
    tail = parts[parts.index('root') + 1:]
    if tail[:1] == ['refcnt'] and len(tail) >= 2:
        tail = tail[2:]
    # Reject unknown options rather than pretend that rollback can restore them.
    valued = {'limit', 'flow_limit', 'flows', 'quantum', 'target', 'interval', 'memory_limit',
              'drop_batch', 'buckets', 'maxrate', 'low_rate_threshold', 'orphan_mask',
              'ce_threshold', 'ce_threshold_selector', 'initial_quantum', 'timer_slack', 'horizon',
              'refill_delay', 'defrate', 'offload_horizon'}
    switches = {'ecn', 'noecn', 'pacing', 'nopacing', 'horizon_drop', 'horizon_cap'}
    restored = []
    i = 0
    while i < len(tail):
        token = tail[i]
        if token in switches:
            restored.append(token)
            i += 1
        elif kind == 'fq' and token == 'bands':
            # tc fq uses exactly three bands and sixteen priority mappings.
            if (len(tail) < i + 19 or tail[i + 1] != '3' or tail[i + 2] != 'priomap'
                    or any(x not in ('0', '1', '2') for x in tail[i + 3:i + 19])):
                raise RuntimeError('Не удалось безопасно восстановить bands/priomap очереди fq.')
            restored.extend(tail[i:i + 19])
            i += 19
        elif kind == 'fq' and token == 'weights':
            weights = tail[i + 1:i + 4]
            if len(weights) != 3 or any(not x.isascii() or not x.isdecimal() for x in weights):
                raise RuntimeError('Не удалось безопасно восстановить weights очереди fq.')
            restored.extend([token, *weights])
            i += 4
        elif token in valued and i + 1 < len(tail) and re.fullmatch(r'[0-9A-Za-z.%/-]+', tail[i + 1]):
            value = tail[i + 1]
            # tc prints packet counts with a p suffix, but accepts plain integers.
            if token in ('limit', 'flow_limit') and re.fullmatch(r'[0-9]+p', value):
                value = value[:-1]
            # fq similarly prints byte counts with b, but parses unsigned integers.
            if kind == 'fq' and token in ('quantum', 'initial_quantum') and re.fullmatch(r'[0-9]+b', value):
                value = value[:-1]
            restored.extend((token, value))
            i += 2
        else:
            raise RuntimeError('Неподдерживаемый параметр очереди: ' + token + '. Изменение отменено.')
    return kind, restored


def tune_cake():
    heading('CAKE — ТЕСТ ОЧЕРЕДИ')
    if 'cake' in current():
        raise RuntimeError('CAKE уже настроен этим инструментом; сначала выполните его откат.')
    iface = default_iface()
    if not iface:
        raise RuntimeError('Маршрут по умолчанию не определён однозначно.')
    animate('Проверяем текущую очередь и возможность отката')
    kind, options = parse_root_qdisc(iface)
    print('  Интерфейс:', iface, '| текущая очередь:', kind)
    print('  CAKE управляет исходящим трафиком интерфейса; скорость подбирается по измерениям.')
    print('  Ограничение влияет на все исходящие соединения. После перезагрузки CAKE не сохраняется.')
    rate = prompt('Скорость в Мбит/с (целое число; 0 — отмена): ')
    if rate == '0':
        return
    if not rate.isascii() or not rate.isdecimal() or not 1 <= int(rate) <= 100000:
        raise ValueError('Укажите целое число от 1 до 100000.')
    if not confirm(f'Установить CAKE на {iface} с ограничением {rate} Мбит/с?'):
        return
    if default_iface() != iface or parse_root_qdisc(iface) != (kind, options):
        raise RuntimeError('Маршрут или очередь изменились во время подготовки; повторите операцию.')
    record = begin('cake', {'iface': iface, 'old_kind': kind, 'old_options': options,
                            'rate': int(rate), 'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip()})
    animate('Применяем CAKE')
    run('tc', 'qdisc', 'replace', 'dev', iface, 'root', 'cake', 'bandwidth', rate + 'mbit')
    qdisc = run('tc', 'qdisc', 'show', 'dev', iface).stdout
    if not re.search(r'\bqdisc cake\b.*\broot\b', qdisc):
        raise RuntimeError('Не удалось подтвердить CAKE; используйте откат.')
    record['status'] = 'active'; save(record)
    notice('CAKE включён временно. Сравните скорость, задержку под нагрузкой и CPU.', 'ok')


def swap_entries():
    lines = Path('/proc/swaps').read_text().splitlines()[1:]
    return [line.split()[0] for line in lines if line.split()]


def tune_swap():
    heading('НАСТРОЙКА SWAP')
    if 'swap' in current():
        raise RuntimeError('Swap уже настроен этим инструментом.')
    mem = meminfo(); total = mem.get('MemTotal', 0); existing = mem.get('SwapTotal', 0)
    print(f'  RAM: {total // 2**20} МБ; текущий swap: {existing // 2**20} МБ')
    if existing:
        notice('На сервере уже есть swap. Автоматическое изменение существующего swap не выполняется.', 'warn')
        print(read('swapon', '--show'))
        return
    if SWAP.exists() or SWAP.is_symlink():
        raise RuntimeError('Целевой файл уже существует; он не будет перезаписан: ' + str(SWAP))
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(STATE, 0o700)
    fs = run('findmnt', '-no', 'FSTYPE', '--target', str(STATE)).stdout.strip()
    if fs not in ('ext4', 'xfs'):
        raise RuntimeError('Автосоздание поддерживает ext4 и xfs; файловая система: ' + fs)
    free = shutil.disk_usage(STATE).free
    # Conservative suggestion, explicitly adjustable. Leave at least 2 GiB and 20% of free disk.
    cap = min(free - 2 * 2**30, int(free * 0.8)) // 2**20
    suggested = min(2048, max(512, total // 2**20), cap)
    if suggested < 256:
        raise RuntimeError('Недостаточно свободного места для безопасного предложения swap.')
    print(f'  ФС: {fs}; свободно: {free // 2**20} МБ; предложение: {suggested} МБ.')
    print('  Будет создан отдельный файл; существующие разделы и файлы не изменяются.')
    raw = prompt(f'Размер в МБ [{suggested}], 0 — отмена: ')
    if raw == '0': return
    if raw and (not raw.isascii() or not raw.isdecimal()): raise ValueError('Нужен целый размер в МБ.')
    size = int(raw or suggested)
    if not 256 <= size <= cap:
        raise ValueError(f'Размер должен быть от 256 до {cap} МБ.')
    if FSTAB.is_symlink() or not FSTAB.is_file():
        raise RuntimeError('/etc/fstab отсутствует или является ссылкой; проверьте вручную.')
    original = FSTAB.read_text(encoding='utf-8')
    if MARK in original or str(SWAP) in original:
        raise RuntimeError('Запись swap уже есть в fstab; проверьте вручную.')
    if not confirm(f'Создать и включить swap {size} МБ и добавить его в fstab?'):
        return
    record = begin('swap', {'path': str(SWAP), 'size_mb': size, 'token': uuid.uuid4().hex})
    # Use O_EXCL: never overwrite an existing file, even if it appeared after inspection.
    fd = os.open(str(SWAP), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    record['inode'] = os.fstat(fd).st_ino; save(record)
    try:
        # Writing zeroes is portable across supported filesystems, including those rejecting fallocate.
        with os.fdopen(fd, 'wb') as stream:
            block = bytes(1024 * 1024)
            for _ in range(size): stream.write(block)
            stream.flush(); os.fsync(stream.fileno())
    except BaseException:
        raise
    run('mkswap', str(SWAP))
    run('swapon', str(SWAP))
    if str(SWAP) not in swap_entries(): raise RuntimeError('Не удалось подтвердить активный swap; используйте откат.')
    entry = f'{SWAP} none swap sw 0 0 {MARK} {record["token"]}'
    record['fstab_entry'] = entry; save(record)
    latest = FSTAB.read_text(encoding='utf-8')
    if latest != original: raise RuntimeError('fstab изменился во время операции; используйте откат.')
    stat = FSTAB.stat()
    atomic_write(FSTAB, latest + ('' if latest.endswith('\n') else '\n') + entry + '\n', stat.st_mode & 0o777)
    os.chown(FSTAB, stat.st_uid, stat.st_gid)
    record['status'] = 'active'; save(record)
    notice(f'Swap {size} МБ создан и включён; запись добавлена в fstab.', 'ok')


def rollback():
    heading('ОТКАТ ИЗМЕНЕНИЯ')
    if not CURRENT.exists():
        notice('Активного изменения нет.', 'warn'); return
    records = current()
    kinds = list(records)
    for i, name in enumerate(kinds, 1):
        print(f'  [{i}] {name}: {records[name].get("status")}')
    selected = prompt('Номер изменения (0 — назад): ')
    if selected == '0': return
    if not selected.isascii() or not selected.isdecimal() or not 1 <= int(selected) <= len(kinds):
        raise ValueError('Неверный номер.')
    kind = kinds[int(selected) - 1]
    record = records[kind]
    print('  Изменение:', kind, '| состояние:', record.get('status'))
    if not confirm('Восстановить предыдущее состояние?'):
        return
    if kind == 'bbr':
        if CONF.is_symlink():
            raise RuntimeError('Файл настроек заменён ссылкой; автоматический откат остановлен.')
        if CONF.exists():
            digest = hashlib.sha256(CONF.read_bytes()).hexdigest()
            if digest != record.get('sha256'):
                raise RuntimeError(f'{CONF} изменён после настройки; автоматический откат остановлен.')
        value = sysctl_get(record['key'])
        if value not in ('bbr', record['previous']):
            raise RuntimeError('Текущий алгоритм изменён извне: ' + value)
        run('sysctl', '-w', record['key'] + '=' + record['previous'])
        if CONF.exists(): CONF.unlink()
    elif kind == 'cake':
        boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        if boot != record['boot_id']:
            notice('Сервер перезагружен; временная очередь уже сброшена. Проверьте tc qdisc вручную.', 'warn')
        else:
            qdisc = run('tc', 'qdisc', 'show', 'dev', record['iface']).stdout
            if record.get('status') == 'prepared' and re.search(r'\bqdisc ' + re.escape(record['old_kind']) + r'\b.*\broot\b', qdisc):
                notice('Предыдущая очередь осталась на месте.', 'info')
            elif not re.search(r'\bqdisc cake\b.*\broot\b', qdisc):
                raise RuntimeError('Очередь изменена извне; автоматический откат остановлен.')
            else:
                run('tc', 'qdisc', 'replace', 'dev', record['iface'], 'root', record['old_kind'], *record['old_options'])
    elif kind == 'swap':
        path = Path(record['path'])
        if path != SWAP or path.is_symlink(): raise RuntimeError('Путь swap изменён; требуется ручная проверка.')
        if path.exists() and path.stat().st_ino != record.get('inode'):
            raise RuntimeError('Файл swap заменён извне; автоматический откат остановлен.')
        if str(path) in swap_entries():
            mem = meminfo(); used = mem.get('SwapTotal', 0) - mem.get('SwapFree', 0)
            if mem.get('MemAvailable', 0) < used + 256 * 2**20:
                raise RuntimeError('Недостаточно доступной RAM для отключения swap; откат остановлен.')
            run('swapoff', str(path))
        entry = record.get('fstab_entry')
        if FSTAB.is_symlink() or not FSTAB.is_file():
            raise RuntimeError('/etc/fstab отсутствует или является ссылкой; проверьте вручную.')
        contents = FSTAB.read_text(encoding='utf-8')
        if entry and entry in contents.splitlines():
            lines = contents.splitlines(keepends=True)
            st = FSTAB.stat()
            atomic_write(FSTAB, ''.join(x for x in lines if x.rstrip('\r\n') != entry), st.st_mode & 0o777)
            os.chown(FSTAB, st.st_uid, st.st_gid)
        elif str(path) in contents:
            raise RuntimeError('Запись swap в fstab изменилась; проверьте её вручную.')
        if path.exists(): path.unlink()
    else:
        raise RuntimeError('Неизвестный тип снимка.')
    del records[kind]
    if records:
        atomic_write(CURRENT, json.dumps(records, ensure_ascii=False, indent=2) + '\n')
    else:
        CURRENT.unlink()
    notice('Откат завершён.', 'ok')


def main():
    if os.geteuid() != 0: sys.exit('Запустите от root: sudo bash install.sh')
    if not sys.stdin.isatty(): sys.exit('Нужен интерактивный терминал.')
    if not sys.platform.startswith('linux'): sys.exit('Поддерживается Linux.')
    lock = open('/run/prism-node-tuning.lock', 'w')
    try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError: sys.exit('Другой экземпляр уже запущен.')
    clear_screen()
    banner()
    while True:
        try:
            system_info()
            heading('ГЛАВНОЕ МЕНЮ')
            for number, label in [('1', 'Проверить сервер'), ('2', 'Включить BBR'),
                                  ('3', 'Настроить swap'), ('4', 'Настроить CAKE'),
                                  ('5', 'Откатить изменение'), ('0', 'Выход')]:
                print(f'  {styled("[" + number + "]", "cyan", True)}  {label}')
            choice = prompt('Выберите действие: ')
            if choice in ('0', 'q', 'exit', 'выход'):
                animate('Завершаем работу')
                clear_screen()
                notice('Скрипт завершён.', 'ok')
                return
            clear_screen()
            if choice == '1': diagnostic()
            elif choice == '2': tune_bbr()
            elif choice == '3': tune_swap()
            elif choice == '4': tune_cake()
            elif choice == '5': rollback()
            else: notice('Выберите пункт меню.', 'warn')
        except (KeyboardInterrupt, EOFError):
            print(); return
        except Exception as exc:
            notice(str(exc), 'error')
            try:
                after = current()
                if any(item.get('status') == 'prepared' for item in after.values()):
                    notice('Есть незавершённое изменение. Проверьте пункт отката.', 'warn')
                elif after:
                    notice('Существующие изменения доступны в пункте отката.', 'info')
            except Exception:
                notice('Не удалось прочитать состояние изменений; проверьте его вручную.', 'warn')
        try:
            prompt('Enter — вернуться в меню ')
            animate('Возвращаемся в меню')
            clear_screen()
        except (KeyboardInterrupt, EOFError):
            print(); return


if __name__ == '__main__':
    main()
