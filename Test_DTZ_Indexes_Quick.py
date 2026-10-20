"""Independent, fast four-game GATG name-index audit; no image decode required.

Usage: python Test_DTZ_Indexes_Quick.py [directory of four supplied GAME.DTZ files]
The originals are never modified and are not bundled with the application.
"""
from __future__ import annotations
from collections import Counter
from pathlib import Path
import hashlib, struct, sys, zlib
from DTZ_Section_Index import inspect_indexed_sections

EXPECTED = {
    'GTA LCS PS2 GAME.DTZ': {'hud': 60, 'generic': 21, 'particle': 117},
    'GTA LCS PSP GAME.DTZ': {'hud': 60, 'generic': 20, 'particle': 117},
    'GTA VCS PS2 GAME.DTZ': {'hud': 68, 'generic': 53, 'particle': 131},
    'GTA VCS PSP GAME.DTZ': {'hud': 72, 'generic': 53, 'particle': 131},
}

def verify_game(file: Path):
    source = file.read_bytes()
    dat = zlib.decompress(source)
    assert dat.startswith(b'GATG')
    sections = inspect_indexed_sections(dat)
    counts = {s:len(x) for s,x in sections.items()}
    assert counts == EXPECTED[file.name], (file.name, counts)
    all_nodes = set()
    # Independent byte-level linkage check instead of trusting the parsed dict.
    group_names = []
    for group,records in sections.items():
        for row in records:
            n = row['record_offset']
            assert n not in all_nodes, (file.name, group, hex(n))
            all_nodes.add(n)
            assert dat[n+0x20:n+0x54].split(b'\0')[0] == row['name'].encode('ascii')
            a = struct.unpack_from('<I', dat, n+0x10)[0]
            assert a == row['asset_pointer'] and a < len(dat)
            group_ptr = struct.unpack_from('<I', dat, n+0x14)[0]
            assert 0x100<group_ptr<len(dat) and dat[group_ptr]==0x06
            record_type = struct.unpack_from('<I', dat, n+0x0c)[0]
            assert record_type == row['record_type']
            group_names.append((group,row['name']))
    # Multiple linked nodes can intentionally reuse the same name. Distinguish
    # by record offset; do not drop or overwrite entries with duplicate labels.
    duplicates={k:v for k,v in Counter(group_names).items() if v>1}
    assert zlib.decompress(zlib.compress(dat, 9)) == dat
    print(f'{file.name}: {len(dat):,} decompressed bytes, ' + ', '.join(f'{g}={c}' for g,c in counts.items()) + 
          f'; {len(all_nodes)} unique linked nodes, {len(duplicates)} repeated labels; SHA256={hashlib.sha256(source).hexdigest()[:16]}')
    for key,n in duplicates.items(): print(f'  repeated label (distinct records): {key[0]}/{key[1]} × {n}')
    return counts

def main():
    root = Path(sys.argv[1]) if len(sys.argv)>1 else Path('/mnt/data')
    for filename in EXPECTED:
        file=root/filename
        if not file.exists():
            print('Missing required reference:',file)
            return 2
        verify_game(file)
    print('PASS: independent second-pass name/linkage audit for LCS PS2, LCS PSP, VCS PS2, VCS PSP.')
    return 0

if __name__=='__main__':
    raise SystemExit(main())
