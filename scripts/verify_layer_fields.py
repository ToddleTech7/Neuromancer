"""图层字段验证：source 三种形态 + 样式字段（P0-0 / P0-1）。

用法（仓库根目录）：
    python scripts/verify_layer_fields.py

为什么单独一个：这两类字段**在公开 precomputed 数据上测不出来**——
precomputed 的 `source` 是字符串，没有 `state`；样式字段要人工改才有值。
所以这里不连任何站点，直接在 about:blank 里用构造的图层对象跑
`R._internals.layerInfo()`，断言映射正确、且空字段不膨胀。

诚实边界：这是**字段映射**的单测，不是“真实 graphene 前端已接上”的证明。
真实环境要另跑 `verify_cave_recorder_e2e.py` / 现场清单（见
docs/私有扩展接入.md 第 5 节）。
"""

import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import cave_session_pack as csp  # noqa: E402
ROOT = HERE.parent
OUT = ROOT / 'results' / 'layer_fields'

# 三种 source 形态：graphene 对象 / precomputed 字符串 / 多源数组
CASES = [
    {'name': 'seg', 'type': 'segmentation', 'visible': True,
     'source': {'url': 'graphene://https://x/seg',
                'state': {'segments': ['1', '2']}},
     'segments': ['1956846'],
     'baseSegmentColoring': True, 'baseSegmentHighlighting': False,
     'selectedAlpha': 0.8, 'notSelectedAlpha': 0.3, 'objectAlpha': 0.6,
     'colorSeed': 1234, 'segmentDefaultColor': '#ff0000',
     'segmentColors': {'1956846': '#00ff00', '1956847': '#0000ff'}},
    {'name': 'img', 'type': 'image', 'visible': False,
     'source': 'precomputed://https://x/em'},
    {'name': 'multi', 'type': 'segmentation',
     'source': [{'url': 'precomputed://a'},
                {'url': 'precomputed://b', 'state': {}}]},
    # 什么都没有的裸图层：不应该被塞 style / source_detail
    {'name': 'bare', 'type': 'image'},
]


def main():
    checks = []

    def check(name, ok, detail=''):
        checks.append({'name': name, 'ok': bool(ok), 'detail': detail})
        print(('  [OK]   ' if ok else '  [FAIL] ') + name
              + (('  ' + str(detail)) if detail else ''))

    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        pg = b.new_page()
        pg.goto('about:blank')
        pg.add_script_tag(path=str(HERE / 'cave_recorder.user.js'))
        out = pg.evaluate("""(cases) => {
          const I = window.__caveRecorder && window.__caveRecorder._internals;
          if (!I) return {err: 'no _internals'};
          const li = I.layerInfo(cases);
          return {
            li,
            plain_obj: I.sourceInfo(cases[0].source),
            plain_str: I.sourceInfo(cases[1].source),
            plain_arr: I.sourceInfo(cases[2].source),
            nil: I.sourceInfo(undefined),
          };
        }""", CASES)
        b.close()

    if out.get('err'):
        print('FAIL:', out['err'])
        return 2

    li = out['li']
    seg, img, multi, bare = li

    print('\n=== P0-0 对象型 source（旧版这里是 null） ===')
    check('graphene 对象 source → source_detail.url',
          (seg.get('source_detail') or {}).get('url')
          == 'graphene://https://x/seg', seg.get('source_detail'))
    check('graphene 对象 source → has_state=true',
          (seg.get('source_detail') or {}).get('has_state') is True)
    check('has_state 不复制 state 本体',
          'state' not in (seg.get('source_detail') or {}))
    check('字符串 source 保持原字段不变',
          img.get('source') == 'precomputed://https://x/em'
          and (img.get('source_detail') or {}).get('url')
          == 'precomputed://https://x/em')
    check('多源数组 → 每项都解析',
          isinstance(multi.get('source_detail'), list)
          and len(multi['source_detail']) == 2
          and multi['source_detail'][1].get('has_state') is True,
          multi.get('source_detail'))
    check('sourceInfo(undefined) → null', out['nil'] is None)

    print('\n=== P0-1 图层样式 ===')
    st = seg.get('style') or {}
    for k, want in [('baseSegmentColoring', True),
                    ('baseSegmentHighlighting', False),
                    ('selectedAlpha', 0.8), ('notSelectedAlpha', 0.3),
                    ('objectAlpha', 0.6), ('colorSeed', 1234),
                    ('segmentDefaultColor', '#ff0000')]:
        check('style.%s = %r' % (k, want), st.get(k) == want, st.get(k))
    check('segmentColors 只记条目数（不塞本体）',
          st.get('segmentColors_count') == 2 and 'segmentColors' not in st,
          st.get('segmentColors_count'))

    print('\n=== 不膨胀 ===')
    check('裸图层没有 style 字段', 'style' not in bare, sorted(bare))
    check('裸图层没有 source_detail 字段', 'source_detail' not in bare)
    check('每条都有既有字段 name/type/visible/segments_count',
          all(all(k in x for k in ('name', 'type', 'visible',
                                   'segments_count')) for x in li))

    ok = all(c['ok'] for c in checks)
    print('\n%d 项检查：%s' % (len(checks), 'PASS' if ok else 'FAIL'))

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / 'summary.json').write_text(json.dumps(
        {'cases': CASES, 'result': li, 'checks': checks, 'pass': ok},
        ensure_ascii=False, indent=1), encoding='utf-8')
    print('summary: %s' % csp.relpath(OUT / 'summary.json'))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
