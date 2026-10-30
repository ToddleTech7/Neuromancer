#!/usr/bin/env python3
"""文档自检：Markdown 相对链接、必需文档、当前文档里的旧文件名。

只检查 docs/、README、scripts/README、results/README；开发记录是历史，不参与旧名检查。
退出码 0/1，可直接进 CI。
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LINK_RE = re.compile(r'\[([^\]]*)\]\(([^)]+)\)')
OLD_NAMES = ('STATUS.md', 'BACKLOG.md', 'DESIGN.md', 'DATA.md',
             'FEATURES.md', 'USAGE.md', 'DEPLOYMENT.md',
             'OPEN_DECISIONS', 'DEVELOPMENT_LOG', 'PLUGIN_GAPS',
             'REQUIREMENTS_INDEX')
REQUIRED = (
    'README.md',
    'docs/目录.md', 'docs/使用说明.md', 'docs/数据格式.md',
    'docs/设计说明.md', 'docs/当前进度.md', 'docs/已定决策.md',
    'docs/评审报告.md', 'docs/v1发布清单.md', 'CHANGELOG.md',
    'scripts/README.md',
)


def md_files():
    files = [ROOT / 'README.md', ROOT / 'scripts' / 'README.md',
             ROOT / 'results' / 'README.md']
    files += list((ROOT / 'docs').rglob('*.md'))
    return [f for f in files if f.exists()]


def main():
    errors = []
    for f in md_files():
        text = f.read_text(encoding='utf-8')
        # 代码块/行内 code 里的 [x](y) 不是链接；先去掉再扫。
        scan = re.sub(r'```.*?```', '', text, flags=re.S)
        scan = re.sub(r'`[^`]*`', '', scan)
        for m in LINK_RE.finditer(scan):
            href = m.group(2).split('#')[0]
            if not href or '://' in href or href.startswith('mailto:'):
                continue
            p = (f.parent / href).resolve()
            if not p.exists():
                line = scan[:m.start()].count('\n') + 1
                errors.append('%s:%d 死链 → %s' % (f.relative_to(ROOT), line, href))
        # 旧名检查：开发记录与评审报告是历史/评审快照，允许出现旧名
        if f.name not in ('开发记录.md', '评审报告.md'):
            for old in OLD_NAMES:
                if old in text:
                    errors.append('%s 仍引用旧名 %s' % (f.relative_to(ROOT), old))
    for rel in REQUIRED:
        if not (ROOT / rel).exists():
            errors.append('缺少必需文档 %s' % rel)
    if errors:
        print('文档自检 FAIL（%d 项）' % len(errors))
        for e in errors[:40]:
            print('  ' + e)
        return 1
    print('文档自检 PASS（%d 篇）' % len(md_files()))
    return 0


if __name__ == '__main__':
    sys.exit(main())
