"""collect_points 单测：“图 ↔ 可恢复点”的绑定规则（L1）。

为什么值得单测：这是一处**会用错但不会报错**的逻辑——
* 快门自带的 state 必须优先（精确），不能被 nearest_time 顶掉；
* 同一个 state（sha 相同）不能被恢复两次；
* 残缺 state（state_partial）必须跳过，而不是拿去重画；
* 没有精确线索时才退到时间就近，且必须有时限（不能乱配）。

跑法：python -m pytest scripts/test_replay_pairs.py -q
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import replay_state as rsq


STATE = {'position': [1, 2, 3], 'layers': [], 'dimensions': [4e-09, 4e-09, 4e-08],
         'layout': '4panel'}
STATE_B = dict(STATE, position=[9, 9, 9])


def _ev(sid, t, sha=None, state=None, partial=None):
    e = {'type': 'viewer.state-change', 'state_id': sid, 't_wall': t,
         'state': STATE if state is None else state}
    if sha:
        e['state_sha'] = sha
    if partial:
        e['state_partial'] = partial
    return e


def _shot(t, state_id=None, sha=None, state=None, image='shots/s1.png'):
    s = {'t_wall': t, 'image_ref': image, 'shot_id': 'shot_1'}
    if state_id is not None:
        s['state_id'] = state_id
    if sha:
        s['state_sha'] = sha
    if state is not None:
        s['state'] = state
    return s


def _run(events, shots):
    points, specs, stats = rsq.collect_points(events, shots)
    return points, [(x['pair_match'], x['point'].get('state_id')) for x in specs], stats


def test_stream_points_and_sha_dedup():
    """同一个 state 被采样两次（sha 相同）只建一个恢复点。"""
    evs = [_ev(1, 100, 'aa'), _ev(2, 200, 'aa'), _ev(3, 300, 'bb')]
    points, _, stats = _run(evs, [])
    assert len(points) == 2, points
    assert [p['state_id'] for p in points] == [1, 3]
    assert stats['n_from_state'] == 2


def test_shot_state_wins_over_time():
    """快门自带 state 时，配对必须是 shot_state（精确），不是就近。"""
    evs = [_ev(1, 100, 'aa')]
    shots = [_shot(1030, state_id=1, sha='cc', state=STATE_B)]
    points, pairs, stats = _run(evs, shots)
    assert [p['source'] for p in points] == ['state', 'shot_state']
    assert pairs == [('shot_state', 1)]
    assert stats['n_from_shot_state'] == 1


def test_shot_sha_matches_stream_no_duplicate_point():
    """快门 state 与流里某个 state 同 sha ⇒ 不重复建点，但仍按精确配对。"""
    evs = [_ev(1, 100, 'aa')]
    shots = [_shot(120, state_id=1, sha='aa', state=STATE)]
    points, pairs, stats = _run(evs, shots)
    assert len(points) == 1, points
    assert pairs == [('shot_state', 1)]
    assert stats['n_from_shot_state'] == 0


def test_state_id_then_nearest_then_drop():
    """无自带 state：先 state_id，再 ≤3s 就近，超时就丢弃（不乱配）。"""
    evs = [_ev(1, 100, 'aa'), _ev(2, 5000, 'bb')]
    shots = [
        _shot(4100, state_id=2),          # state_id 命中
        _shot(7200, state_id=99),         # 无 id，最近点 5000（差 2.2s）⇒ nearest
        _shot(9000, state_id=98),         # 差 4s > 3s ⇒ 丢弃
    ]
    _, pairs, _ = _run(evs, shots)
    assert pairs == [('state_id', 2), ('nearest_time', 2)]


def test_partial_state_skipped_and_counted():
    """残缺 state 不能拿去重画，但必须计数（不静默吞掉）。"""
    evs = [_ev(1, 100, 'aa', partial='position'), _ev(2, 200, 'bb')]
    points, _, stats = _run(evs, [])
    assert len(points) == 1
    assert points[0]['state_id'] == 2
    assert stats['n_partial'] == 1


def test_checkpoint_fallback():
    """完全没有 state-change 时，才退到 checkpoint 剖点。"""
    cps = [{'type': 'viewer.checkpoint', 'state_id': 7, 't_wall': 50,
            'state': STATE, 'viewpoint': {'position': [1, 2, 3]}}]
    points, _, stats = _run(cps, [])
    assert [p['source'] for p in points] == ['checkpoint']
    assert stats['n_from_state'] == 0


def test_shot_without_image_is_not_a_pair():
    """没落盘的图不参与配对（否则会拿空路径去比像素）。"""
    evs = [_ev(1, 100, 'aa')]
    shots = [{'t_wall': 100, 'image_ref': None, 'shot_id': 'shot_x'}]
    _, pairs, _ = _run(evs, shots)
    assert pairs == []


# --- 选中集回放（state 快照里的 segments → sel 指令） ----------------------
# 为什么值得单测：选中集是**独立于视角的 UI 状态**。它不在 pointer/wheel 里，
# 也不在起点 state 快照里；漏了它，回放端就“什么都没选中”，而 NG 恰好按
# “选中为空”渲染（显示全部分割、3D 不画 mesh），画面与录制端相反。
# 注意：`selection.change` 记的是**悬停的段**，不是选中集。

def test_segments_of_state_takes_only_segmentation_layers():
    import replay_manual_session as rms
    st = {'layers': [
        {'name': 'em', 'type': 'image'},
        {'name': 'autoseg', 'type': 'segmentation',
         'segments': ['11', '!22']},
    ]}
    assert rms.segments_of_state(st) == {'autoseg': ['11', '22']}


def test_segments_timeline_dedups_unchanged_sets():
    import replay_manual_session as rms
    def ev(t, segs):
        return {'type': 'viewer.state-change', 't_wall': t,
                'state': {'layers': [{'name': 'autoseg', 'type': 'segmentation',
                                      'segments': segs}]}}
    tl = rms.segments_timeline([
        ev(100, []), ev(200, ['1']), ev(300, ['1']), ev(400, ['1', '2']),
        {'type': 'input.pointer', 't_wall': 500},
    ])
    assert [x['t_wall'] for x in tl] == [100, 200, 400]
    # 空集也必须占一个点："从有选中变回空"不重放，回放端会一直挂着旧选中
    assert tl[0]['layers'] == {'autoseg': []}
    assert tl[2]['layers'] == {'autoseg': ['1', '2']}
