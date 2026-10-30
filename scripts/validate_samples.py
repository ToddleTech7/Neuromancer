"""样本卡数据合约校验器。

校验内容：
  1. schemas/sample_v0.1.schema.json 可加载
  2. 每个 sample_*/sample.json 通过 JSON Schema 校验
  3. observation.images 中每个图像文件存在
  4. index.jsonl / skipped.jsonl 格式与目录一致性
      （index 记录、sample 目录、实际文件三者一一对应）

用法（仓库根目录）：
  python3 scripts/validate_samples.py                  # 默认 results/dataset_v0
  python3 scripts/validate_samples.py results/export_demo
退出码：0 全部通过；1 存在错误（打印前 40 条）。
"""

import argparse
import json
import sys
from pathlib import Path

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / 'schemas' / 'sample_v0.1.schema.json'


def load_schema(path=None):
    return json.loads(Path(path or SCHEMA_PATH).read_text(encoding='utf-8'))


def _read_jsonl(path, errors, label):
    rows = []
    try:
        text = path.read_text(encoding='utf-8')
    except Exception as err:
        errors.append(f'{label}: 读取失败 {type(err).__name__}')
        return rows
    for lineno, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            errors.append(f'{label}: 第 {lineno} 行不是合法 JSON')
    return rows


def validate_dataset(dir_path, schema=None):
    """返回 {'ok', 'n_samples', 'n_skipped', 'errors', 'stats'}。"""
    dir_path = Path(dir_path)
    errors = []
    stats = {}
    if not dir_path.is_dir():
        return {'ok': False, 'n_samples': 0, 'n_skipped': 0,
                'errors': [f'目录不存在: {dir_path}'], 'stats': {}}
    try:
        schema = schema or load_schema()
    except Exception as err:
        return {'ok': False, 'n_samples': 0, 'n_skipped': 0,
                'errors': [f'schema 加载失败: {type(err).__name__} '
                           f'{str(err)[:80]}'], 'stats': {}}
    validator = Draft202012Validator(schema)

    index_rows = _read_jsonl(dir_path / 'index.jsonl', errors,
                             'index.jsonl')
    index_names = set()
    for r in index_rows:
        d = str(r.get('dir') or '')
        if not d:
            errors.append('index.jsonl: 缺少 dir 字段')
            continue
        name = Path(d).name
        index_names.add(name)
        if not (dir_path / name).is_dir():
            errors.append(f'index.jsonl: 记录的目录不存在 {d}')
        if r.get('state') not in ('later_merged', 'later_split', 'latest',
                                  None):
            errors.append(f'index.jsonl: op {r.get("op_id")} 非法 state'
                          f' = {r.get("state")!r}')

    n = 0
    sample_dirs = sorted(p for p in dir_path.glob('sample_*') if p.is_dir())
    for sd in sample_dirs:
        sp = sd / 'sample.json'
        if not sp.exists():
            errors.append(f'{sd.name}: 缺少 sample.json')
            continue
        try:
            sample = json.loads(sp.read_text(encoding='utf-8'))
        except Exception as err:
            errors.append(f'{sd.name}: sample.json 解析失败 '
                          f'{type(err).__name__}')
            continue
        for e in sorted(validator.iter_errors(sample),
                        key=lambda x: list(x.path)):
            loc = '/'.join(str(p) for p in e.path) or '(root)'
            errors.append(f'{sd.name}: {loc}: {e.message}')
        images = ((sample.get('observation') or {}).get('images') or {})
        for key, fname in images.items():
            if isinstance(fname, str) and not (sd / fname).exists():
                errors.append(f'{sd.name}: 图像缺失 {key}={fname}')
        state = (sample.get('outcome') or {}).get('state')
        stats[state] = stats.get(state, 0) + 1
        n += 1

    actual_names = {sd.name for sd in sample_dirs}
    for m in sorted(actual_names - index_names):
        errors.append(f'index.jsonl: 缺失样本目录记录 {m}')
    for m in sorted(index_names - actual_names):
        errors.append(f'index.jsonl: 记录了不存在的目录 {m}')

    n_skipped = 0
    skp = dir_path / 'skipped.jsonl'
    if skp.exists():
        for i, r in enumerate(_read_jsonl(skp, errors, 'skipped.jsonl'), 1):
            if 'op_id' not in r or 'reason' not in r:
                errors.append(f'skipped.jsonl: 第 {i} 行缺少 op_id/reason')
            else:
                n_skipped += 1

    return {'ok': not errors, 'n_samples': n, 'n_skipped': n_skipped,
            'errors': errors, 'stats': stats}


def main():
    ap = argparse.ArgumentParser(description='样本卡数据合约校验器')
    ap.add_argument('dir', nargs='?',
                    default=str(ROOT / 'results' / 'dataset_v0'))
    ap.add_argument('--schema', default=str(SCHEMA_PATH))
    args = ap.parse_args()

    result = validate_dataset(args.dir, load_schema(args.schema))
    print(f'目录: {args.dir}')
    print(f"样本 {result['n_samples']} 条 / 跳过 "
          f"{result['n_skipped']} 条")
    print('结局状态分布:', result['stats'])
    if result['errors']:
        print(f"\n发现 {len(result['errors'])} 个问题（前 40 条）:")
        for e in result['errors'][:40]:
            print('  -', e)
        print('RESULT: FAIL')
        sys.exit(1)
    print('RESULT: PASS')


if __name__ == '__main__':
    main()
