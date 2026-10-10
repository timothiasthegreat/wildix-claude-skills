#!/usr/bin/env python3
"""Package skills/<name>/ into dist/<name>.skill (a zip with <name>/ as the top folder).

Usage:
  python build_skills.py              # build every skill
  python build_skills.py <name> ...   # build only the named skills
"""
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SKILLS = ROOT / 'skills'
DIST = ROOT / 'dist'

# Development-only content that doesn't belong in the installed skill.
EXCLUDE_DIRS = {'__pycache__', 'tests'}
EXCLUDE_FILES = {'README.md'}

# Files a skill uses from another skill, copied into its package at build time (source paths under skills/,
# placed in the same subfolder of the packaged skill). The source stays the single copy in the repo.
SHARED = {
    'wildix-config-docs': ['wildix-config-review/scripts/wildix_config_parser.py',
                           'wildix-config-review/scripts/lua51.py'],
    'wildix-call-flow-sim': ['wildix-config-review/scripts/wildix_config_parser.py',
                             'wildix-config-review/scripts/lua51.py'],
}


def build(skill_dir):
    if not (skill_dir / 'SKILL.md').is_file():
        raise SystemExit(f'{skill_dir} has no SKILL.md')
    DIST.mkdir(exist_ok=True)
    out = DIST / f'{skill_dir.name}.skill'
    files = sorted(
        f for f in skill_dir.rglob('*')
        if f.is_file()
        and not EXCLUDE_DIRS.intersection(f.relative_to(skill_dir).parts)
        and not (f.parent == skill_dir and f.name in EXCLUDE_FILES)
    )
    shared = [SKILLS / s for s in SHARED.get(skill_dir.name, [])]
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
        for f in files:
            z.write(f, f.relative_to(SKILLS).as_posix())
        for f in shared:
            if not f.is_file():
                raise SystemExit(f'{skill_dir.name}: shared file {f} not found')
            sub = f.relative_to(SKILLS).parts[1:]  # drop the source skill's folder
            z.write(f, '/'.join((skill_dir.name, *sub)))
    print(f'{out.relative_to(ROOT)}: {len(files) + len(shared)} files')


def main():
    names = sys.argv[1:] or sorted(p.name for p in SKILLS.iterdir() if p.is_dir())
    for name in names:
        build(SKILLS / name)


if __name__ == '__main__':
    main()
