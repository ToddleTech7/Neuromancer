"""动作回放里“动作有效性”判据的单测。

为什么值得单测：这是**防假绿**的那道门。没有它，“重放误差 0”可能只是
“两边都没动”——测试会给出一个漂亮的 0，而什么都没验到。

跑法：python -m pytest scripts/test_action_effects.py -q
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import verify_action_replay as v  # noqa: E402


def _st(pos=(0, 0, 0), scale=6.0):
    return {'position': list(pos), 'crossSectionScale': scale,
            'layers': [], 'dimensions': [4, 4, 40], 'layout': '4panel'}


def _anchors(**kw):
    base = {k: _st() for k in ('pre', 'g_drag1', 'g_wheel', 'g_click', 'g_drag2')}
    base.update(kw)
    return base


def test_zero_drag_is_not_effective():
    """拖动没改变位置 ⇒ 不算“有动作”（否则重放误差 0 是假绿）。"""
    eff = v.action_effects(_anchors())
    assert eff['g_drag1']['effective'] is False
    assert eff['g_drag1']['pos_delta_nm'] == 0.0


def test_real_drag_is_effective():
    a = _anchors(g_drag1=_st(pos=(500, 0, 0)))
    eff = v.action_effects(a)
    assert eff['g_drag1']['effective'] is True
    assert eff['g_drag1']['pos_delta_nm'] == 500.0


def test_wheel_needs_scale_change():
    """滚轮的关键是尺度变化；只动几 nm 的位置不算缩放。"""
    a = _anchors(g_wheel=_st(pos=(0, 0, 3.5), scale=6.0))
    assert v.action_effects(a)['g_wheel']['effective'] is False

    b = _anchors(g_wheel=_st(scale=0.54))
    e = v.action_effects(b)['g_wheel']
    assert e['effective'] is True
    assert e['scale_rel'] is not None and e['scale_rel'] > 0.9


def test_click_is_effective_by_definition():
    """点击的预期就是“视角不变”，所以它永远算有动作（真假断言在重放侧）。"""
    assert v.action_effects(_anchors())['g_click']['effective'] is True


def test_rel_and_pos_delta_handle_bad_input():
    assert v._pos_delta(None, None) is None
    assert v._pos_delta({'position': [0, 0]}, {'position': [0, 0, 0]}) is None
    assert v._rel(0, 1) is None
    assert v._rel(None, 1) is None
