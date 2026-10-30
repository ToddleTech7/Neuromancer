"""validate_samples 数据合约校验器单测。"""

import json
import tempfile
import unittest
from pathlib import Path

import validate_samples as vs


def make_sample(op_id=1001, state='latest'):
    return {
        'schema': 'neuromancer.sample.v0.1',
        'source': {'datastack': 'minnie65_public', 'op_id': op_id,
                   'op_type': 'merge', 'user': 'u1',
                   'timestamp_utc': '2025-12-08 07:15:10+00:00'},
        'observation': {
            'render_params': {'S_nm': 3584.0, 'mip': 1},
            'images': {'em_xy': 'em_xy.png', 'em_xz': 'em_xz.png',
                       'em_yz': 'em_yz.png',
                       'overlay_xz': 'overlay_xz.png'},
            'interface_point_nm': [1.0, 2.0, 3.0],
            'viewpoint': None, 'note': 'test',
        },
        'action': {'type': 'merge',
                   'before_root_ids': [10, 20], 'after_root_ids': [30],
                   'source_coords': None, 'sink_coords': None},
        'segments': {'segment1_id': 10, 'segment2_id': 20,
                     'semantics': 'test'},
        'outcome': {'state': state, 'survival_seconds': 12.5,
                    'next_change': None, 'checked_at': None},
        'process': {'session_id': None, 'human_decision': 'accept',
                    'human_note': None, 'note': 'test'},
        'labels': {'action': 'merge', 'outcome_state': state,
                   'usage_hint': 'test'},
    }


def write_dataset(root, sample=None, drop_image=False, index=True,
                  index_extra=False):
    sample = sample or make_sample()
    sd = root / f"sample_{sample['source']['op_id']}"
    sd.mkdir(parents=True)
    (sd / 'sample.json').write_text(
        json.dumps(sample, ensure_ascii=False), encoding='utf-8')
    for fn in ('em_xy.png', 'em_xz.png', 'em_yz.png', 'overlay_xz.png'):
        if drop_image and fn == 'overlay_xz.png':
            continue
        (sd / fn).write_bytes(b'x')
    if index:
        rows = [{'op_id': sample['source']['op_id'],
                 'state': sample['outcome']['state'],
                 'mask_hit_px': 1, 'dir': f'results/x/{sd.name}'}]
        if index_extra:
            rows.append({'op_id': 9999, 'state': 'latest',
                         'mask_hit_px': 1, 'dir': 'results/x/sample_9999'})
        (root / 'index.jsonl').write_text(
            '\n'.join(json.dumps(r) for r in rows) + '\n',
            encoding='utf-8')
    return sd


class TestValidateSamples(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_schema_file_loads(self):
        schema = vs.load_schema()
        self.assertEqual(schema['title'], 'Neuromancer 样本卡 v0.1')

    def test_valid_dataset_passes(self):
        write_dataset(self.root, make_sample())
        r = vs.validate_dataset(self.root)
        self.assertTrue(r['ok'], r['errors'])
        self.assertEqual(r['n_samples'], 1)
        self.assertEqual(r['stats'], {'latest': 1})

    def test_missing_image_fails(self):
        write_dataset(self.root, make_sample(), drop_image=True)
        r = vs.validate_dataset(self.root)
        self.assertFalse(r['ok'])
        self.assertTrue(any('图像缺失' in e for e in r['errors']))

    def test_bad_state_enum_fails(self):
        s = make_sample()
        s['outcome']['state'] = 'bogus'
        write_dataset(self.root, s)
        r = vs.validate_dataset(self.root)
        self.assertFalse(r['ok'])
        self.assertTrue(any('outcome/state' in e for e in r['errors']))

    def test_missing_required_field_fails(self):
        s = make_sample()
        del s['labels']
        write_dataset(self.root, s)
        r = vs.validate_dataset(self.root)
        self.assertFalse(r['ok'])
        self.assertTrue(any("'labels' is a required property" in e
                            for e in r['errors']))

    def test_index_extra_dir_fails(self):
        write_dataset(self.root, make_sample(), index_extra=True)
        r = vs.validate_dataset(self.root)
        self.assertFalse(r['ok'])
        self.assertTrue(any('不存在的目录' in e for e in r['errors']))

    def test_missing_index_fails(self):
        write_dataset(self.root, make_sample(), index=False)
        r = vs.validate_dataset(self.root)
        self.assertFalse(r['ok'])

    def test_skipped_rows_checked(self):
        write_dataset(self.root, make_sample())
        (self.root / 'skipped.jsonl').write_text(
            json.dumps({'op_id': 5, 'reason': 'x'}) + '\n',
            encoding='utf-8')
        r = vs.validate_dataset(self.root)
        self.assertTrue(r['ok'], r['errors'])
        self.assertEqual(r['n_skipped'], 1)


if __name__ == '__main__':
    unittest.main()
