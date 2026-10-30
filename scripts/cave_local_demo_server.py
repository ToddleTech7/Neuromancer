"""本地 Neuroglancer 演示/验证环境（真实前端，pip 包内置，独立进程）。

用途：不依赖自建 CAVE / 公网前端，本地启动一个真实的 Neuroglancer
前端，供 recorder 自动化验证（verify_cave_recorder_e2e.py）与人工
演示使用。

为什么要独立进程：neuroglancer 包导入后会（经 tornado）改写 Windows
asyncio 策略，导致同进程 Playwright 无法创建子进程；因此 server 与
自动化驱动分离。

输出协议（stdout，逐行）：
    VIEWER_URL=<url>
    READY
    ERR <...>        （指令执行失败时）

指令协议（stdin，逐行）：
    dims <sx> <sy> <sz>     设置三轴坐标系（x,y,z 尺度，nm）
    pos  <x> <y> <z>        设置十字位置（voxel 坐标，即 dims.scales 单位）
    img  <name> <url>       添加图像层（如 precomputed://https://...）
    seg  <name> <url>       添加分割层（graphene:// 或 precomputed://）
    sel  <name> [id ...]    设置分割层选中集（高亮显示；空则清空）
    layer <name>            设置活动层（鼠标点击作用的目标层）
    cross <scale>           设置 crossSectionScale
    proj  <scale>           设置 projectionScale
    orient <w> <x> <y> <z>  设置 projectionOrientation
    state <json>            整份 viewer state 恢复（R1 回放用；只设可安全映射的字段）

实测注意（2026-09-29）：
- viewer.state 是只读镜像，必须经 txn() 事务修改；
- Neuroglancer position 语义是 voxel 坐标（以 dims.scales 为单位）；
- GCS 数据源须用 precomputed://https://storage.googleapis.com/... 的
  https 形式；gs:// 方言只存在于 Google 内部构建。
"""

import sys
import json
import os

import neuroglancer

v = neuroglancer.Viewer()
print('VIEWER_URL=' + v.get_viewer_url(), flush=True)
print('READY', flush=True)

_seg_src = {}   # 分割层 name → source（sel 重建层时用）
# 3D mesh 渲染倍率（NG 默认 10）。CAVE_MESH_SCALE=0 关闭 mesh 渲染：
# 回放端一旦有选中段就会去拉 mesh 分片，页面忙时输入重放会慢到无法接受。
_MESH_SCALE = float(os.environ.get('CAVE_MESH_SCALE', '10'))


def _src_key(v):
    if isinstance(v, (list, tuple)):
        v = v[0] if v else None
    if isinstance(v, dict):
        v = v.get('url')
    return str(v) if v is not None else None


def _apply_layer_props(lay, l):
    """把 state 里的样式/可见性/透明度/选中集写到一个已存在的层上。"""
    style = {}
    if isinstance(l.get('style'), dict):
        style.update(l['style'])
    for k in ('baseSegmentColoring', 'baseSegmentHighlighting',
              'selectedAlpha', 'notSelectedAlpha', 'objectAlpha',
              'colorSeed', 'segmentDefaultColor'):
        if l.get(k) is not None:
            style[k] = l[k]
    for key, attr in (('baseSegmentColoring', 'base_segment_coloring'),
                      ('baseSegmentHighlighting', 'base_segment_highlighting'),
                      ('selectedAlpha', 'selected_alpha'),
                      ('notSelectedAlpha', 'not_selected_alpha'),
                      ('objectAlpha', 'object_alpha'),
                      ('colorSeed', 'color_seed'),
                      ('segmentDefaultColor', 'segment_default_color')):
        if key in style:
            try:
                setattr(lay, attr, style[key])
            except Exception:
                pass
    try:
        lay.visible = (l.get('visible') is not False)
    except Exception:
        pass
    try:
        lay.opacity = (float(l['opacity'])
                       if isinstance(l.get('opacity'), (int, float)) else 1.0)
    except Exception:
        pass
    if 'segmentation' in str(l.get('type') or '').lower():
        try:
            lay.segments = [int(str(x).lstrip('!'))
                            for x in (l.get('segments') or [])]
        except Exception:
            pass


def _apply_state(s, js):
    """把 recorder 记录的 viewer state 恢复成可观看的视图（R1）。

    只映射能安全重建的字段：坐标系/位置/尺度/朝向/布局/图层与选中。
    不认识的字段直接忽略（不假报"已完整还原"）。

    关键不变量：**缺省即默认**。state 里没有这个键，说明当时该字段就是默认值；
    回放端必须先复位，否则会沿用**上一个回放点**的值（脏状态泄漏）——
    位置对得上、画面却不对，正是这种错最难发现。
    """
    # ---- 1) 先复位那些"缺省即默认"的字段 ----
    for key in ('crossSectionScale', 'projectionScale',
                'crossSectionOrientation', 'projectionOrientation',
                'selectedLayer'):
        if js.get(key) is None:
            try:
                setattr(s, key, None)
            except Exception as err:
                print('ERR reset', key, type(err).__name__, str(err)[:80],
                      flush=True)
    # 切片可见性/局部视口/切片厚度也影响画面；旧版只复位尺度/朝向。
    if js.get('showSlices') is None:
        try:
            s.showSlices = True
        except Exception:
            pass
    if js.get('partialViewport') is None:
        try:
            s.partialViewport = [0, 0, 1, 1]
        except Exception:
            pass
    if js.get('crossSectionDepth') is None:
        try:
            s.crossSectionDepth = None
        except Exception:
            pass
    if isinstance(js.get('showSlices'), bool):
        try:
            s.showSlices = js['showSlices']
        except Exception:
            pass
    if isinstance(js.get('partialViewport'), (list, tuple)) \
            and len(js['partialViewport']) == 4:
        try:
            s.partialViewport = [float(x) for x in js['partialViewport']]
        except Exception:
            pass
    if isinstance(js.get('crossSectionDepth'), (int, float)):
        try:
            s.crossSectionDepth = float(js['crossSectionDepth'])
        except Exception:
            pass

    dims = js.get('dimensions')
    if isinstance(dims, dict) and dims:
        names = list(dims.keys())
        scales, units = [], []
        for n in names:
            e = dims[n]
            if isinstance(e, list) and len(e) >= 2:
                scales.append(float(e[0]))
                units.append(str(e[1]))
            else:
                scales.append(float(e))
                units.append('nm')
        s.dimensions = neuroglancer.CoordinateSpace(
            names=names, units=units, scales=scales)
    if js.get('position'):
        s.position = [float(x) for x in js['position']]
    else:
        # 没有 position 就没法定位；保留原值并明确报错，不假装恢复
        print('ERR state: 缺 position，位置沿用当前值', flush=True)
    for key in ('crossSectionScale', 'projectionScale'):
        if isinstance(js.get(key), (int, float)):
            setattr(s, key, float(js[key]))
    for key in ('crossSectionOrientation', 'projectionOrientation'):
        v = js.get(key)
        if isinstance(v, list) and len(v) == 4:
            try:
                setattr(s, key, [float(x) for x in v])
            except Exception:
                pass
    if js.get('layout'):
        try:
            s.layout = js['layout']
        except Exception:
            pass
    else:
        # 本地 viewer 创建时的默认布局；state 里没写就是“当时用默认”
        try:
            s.layout = '4panel'
        except Exception:
            pass
    layers = {}
    raw_by_name = {}
    for l in (js.get('layers') or []):
        try:
            name = l.get('name') or 'layer'
            src = l.get('source')
            if isinstance(src, list):
                src = src[0] if src else None
            if not src:
                continue
            typ = str(l.get('type') or '').lower()
            if 'segmentation' in typ:
                lay = neuroglancer.SegmentationLayer(
                    source=src,
                    segments=[int(str(x).lstrip('!'))
                              for x in (l.get('segments') or [])],
                    # mesh_render_scale=0 关闭 3D mesh 渲染。为什么需要：一旦层里有
                    # 选中段，NG 就去拉 multilod draco 分片，页面一忙，回放的
                    # mouse.move / evaluate 全部变慢甚至挂死（实测全量回放卡住）。
                    # 默认保持 10（与 NG 一致），需要提速/稳定时用 CAVE_MESH_SCALE=0。
                    mesh_render_scale=_MESH_SCALE)
                _seg_src[name] = src
            else:
                lay = neuroglancer.ImageLayer(source=src)
            _apply_layer_props(lay, l)
            raw_by_name[name] = l
            layers[name] = lay
        except Exception as err:
            print('ERR layer', type(err).__name__, str(err)[:100], flush=True)
    # 记录里 layers 为空 = 当时没有图层；必须清掉上一个回放点的层，
    # 否则会沿用旧层（缺省即默认）。
    # 图层集合没变时**复用现有层**，只更新属性：每换一个 state 就 clear()
    # 会让分割数据重新加载，冷启动时前几张图只剩 EM。
    # 集合或 source 真变了才清空重建。
    if 'layers' in js or layers:
        current = {}
        try:
            for old_lay in s.layers:
                n = getattr(old_lay, 'name', None)
                if n:
                    current[n] = old_lay
        except Exception:
            current = {}
        same = (set(current) == set(layers)
                and all(_src_key(getattr(current[n], 'source', None))
                        == _src_key(raw_by_name.get(n, {}).get('source'))
                        for n in layers))
        if same:
            for name, old_lay in current.items():
                _apply_layer_props(old_lay, raw_by_name.get(name) or {})
        else:
            try:
                s.layers.clear()
            except Exception as err:
                print('ERR layers clear', type(err).__name__, str(err)[:80],
                      flush=True)
            for name, lay in layers.items():
                s.layers[name] = lay
    if js.get('selectedLayer'):
        try:
            s.selectedLayer = js['selectedLayer']
        except Exception:
            pass

def handle(line):
    parts = line.split()
    if not parts:
        return
    cmd = parts[0]
    # 注意：v.state 是只读镜像（TrackableState 的 _readonly 包装），
    # 必须经 txn() 事务修改，退出时自动提交并广播到浏览器。
    with v.txn() as s:
        if cmd == 'dims':
            s.dimensions = neuroglancer.CoordinateSpace(
                names=['x', 'y', 'z'], units='nm',
                scales=[float(x) for x in parts[1:4]])
        elif cmd == 'pos':
            s.position = [float(x) for x in parts[1:4]]
        elif cmd == 'img':
            s.layers[parts[1]] = neuroglancer.ImageLayer(
                source=parts[2])
        elif cmd == 'seg':
            _seg_src[parts[1]] = parts[2]
            s.layers[parts[1]] = neuroglancer.SegmentationLayer(
                source=parts[2])
        elif cmd == 'sel':
            name = parts[1]
            ids = [int(x) for x in parts[2:]]
            src = _seg_src.get(name)
            if src is None:
                print(f'ERR sel: 层 {name} 未加载', flush=True)
                return
            # 回放选区时**只改 segments，不重建层**：重建会丢掉层的其它属性、
            # 并且把已加载的数据全部作废（回放会反复闪/重载）。
            lay = None
            try:
                lay = s.layers[name]
            except Exception:
                lay = None
            if lay is not None:
                try:
                    lay.segments = ids
                    return
                except Exception as err:
                    print(f'ERR sel set: {type(err).__name__} {str(err)[:80]}',
                          flush=True)
            s.layers[name] = neuroglancer.SegmentationLayer(
                source=src, segments=ids, mesh_render_scale=_MESH_SCALE)
        elif cmd == 'layer':
            s.selectedLayer = parts[1]
        elif cmd == 'cross':
            s.crossSectionScale = float(parts[1])
        elif cmd == 'proj':
            s.projectionScale = float(parts[1])
        elif cmd == 'orient':
            s.projectionOrientation = [float(x) for x in parts[1:5]]
        elif cmd == 'state':
            _apply_state(s, json.loads(line[len('state '):]))


for line in sys.stdin:
    try:
        handle(line.strip())
    except Exception as err:
        print('ERR', type(err).__name__, str(err)[:120], flush=True)
