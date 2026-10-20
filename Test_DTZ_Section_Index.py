"""Headless regression for the native GATG section selector and index parser."""
from __future__ import annotations
import os
from pathlib import Path
import struct
from DTZ_Section_Index import inspect_indexed_sections, section_filter_items


def run_self_test():
    data=bytearray(0x1200)
    data[:4]=b'GATG'
    catalog=0x100
    data[0x300] = 0x06
    struct.pack_into('<II', data, catalog-8, 0x300, 1)
    data[catalog:catalog+4]=b'hud\0'
    node=0x500
    struct.pack_into('<III', data, node+0x0c, 2, 0x600, 0x300)
    data[node+0x20:node+0x2b]=b'radar_flag\0'
    catalog_items=inspect_indexed_sections(bytes(data))
    assert list(catalog_items)==['hud']
    assert [(r['name'],r['record_offset']) for r in catalog_items['hud']]==[('radar_flag',node)]
    assert len(section_filter_items([],catalog_items,selected='hud'))==1
    assert not section_filter_items([],catalog_items,selected='menu')
    # Reject fake label without type-one descriptor and unrelated linked nodes.
    struct.pack_into('<I',data,catalog-4,0)
    assert not inspect_indexed_sections(bytes(data))
    actual=os.environ.get('GTA_LCS_DTZ_REFERENCE')
    if actual:
        from texture_formats import DTZDocument
        d=DTZDocument(Path(actual).read_bytes())
        assert len([1 for grp,t in d.entries if grp=='menu'])>=9
        raw=inspect_indexed_sections(d.data)
        assert len(raw.get('hud',[]))>=60
        assert any(x['name']=='radar_north' for x in raw['hud'])
        assert len(section_filter_items(d.entries,d.indexed_sections,selected='menu'))>=9
        assert any(row[0]=='texture' for row in section_filter_items(d.entries,d.indexed_sections,selected='hud'))
    print('GATG section index tests passed: verified named HUD resources, filter separation and false-marker rejection.')

if __name__=='__main__':run_self_test()
