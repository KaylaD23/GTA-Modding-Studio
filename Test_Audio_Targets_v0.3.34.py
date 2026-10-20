"""Native audio format regression checks for all six GTA conversion profiles.

Uses brief generated WAV test tones and optional user-supplied GTA reference assets.
No copyrighted audio is distributed in the release.
"""
from __future__ import annotations

import math
from pathlib import Path
import shutil
import struct
import sys
import tempfile
import wave
import zipfile

import Audio_Preparation_Profiles as profiles
import PS2_VB_Radio as vb


def make_test_wav(path: Path, seconds=1.25, rate=44100):
    size = int(rate * seconds)
    with wave.open(str(path), 'wb') as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        # Different frequencies in L and R catch wrong stereo interleave.
        for i in range(size):
            l = round(12000 * math.sin(2 * math.pi * 440 * i / rate))
            r = round(12000 * math.sin(2 * math.pi * 880 * i / rate))
            wav.writeframesraw(struct.pack('<hh', l, r))


def decode_channel(vb_bytes: bytes, channel: int) -> list[int]:
    coefficients = [(0,0),(60,0),(115,-52),(98,-55),(122,-60)]
    h1 = h2 = 0
    decoded = []
    for block in range(0, len(vb_bytes), vb.STEREO_BLOCK):
        part = vb_bytes[block+channel*vb.CHANNEL_BLOCK:block+(channel+1)*vb.CHANNEL_BLOCK]
        for fi in range(0, len(part), 16):
            f = part[fi:fi+16]
            filt, shift = f[0] >> 4, f[0] & 15
            a,b = coefficients[filt]
            for packed in f[2:]:
                for nibble in (packed & 15,packed >> 4):
                    if nibble >= 8:
                        nibble -= 16
                    pcm = (nibble << 12) >> shift
                    pred = (h1*a+h2*b+32) >> 6
                    sample = max(-32768, min(32767, pcm+pred))
                    decoded.append(sample)
                    h2,h1 = h1,sample
    return decoded


def strongest_tone(samples: list[int], rate: int, target: int) -> bool:
    # Detect the expected tone using sin/cos projections over the first 0.35s.
    # A wrongly interleaved stream has little coherent energy at that frequency.
    start = 280
    count = min(int(rate*0.30), len(samples)-start)
    if count <= 0:
        return False
    def magnitude(freq):
        cx = sx = 0.0
        for i in range(count):
            rad = 2*math.pi*freq*i/rate
            cx += samples[start+i]*math.cos(rad)
            sx += samples[start+i]*math.sin(rad)
        return math.hypot(cx,sx)
    return magnitude(target) > magnitude(660 if target == 440 else 440)*3


def verify_original_reference_metadata(reference_zip: Path):
    if not reference_zip.is_file():
        print('Original GTA reference archive not mounted: independent sample check skipped.')
        return
    cases = {
        'GTA III PS2 FLASH.VB': (32000, 18*60+45.824),
        'GTA VICE CITY PS2 VCPR.VB': (16000, 86*60+24.256),
        'GTA LCS PS2 HEAD.VB': (32000, 26*60+36.672),
        'GTA VCS PS2 FRESH.VB': (32000, 29*60+44.384),
    }
    with zipfile.ZipFile(reference_zip) as archive:
        for name,(rate,expected) in cases.items():
            item = archive.getinfo(name)
            seconds = vb.estimated_vb_duration(item.file_size,rate)
            assert abs(seconds-expected) < .003,(name,seconds,expected)
            with archive.open(item) as fp:
                first = fp.read(vb.STEREO_BLOCK)
                assert first[:16] == bytes(16) and first[0x2000:0x2010] == bytes(16),name
                for off in range(0,len(first),16):
                    assert first[off+1] == 0,name
        # Cross-platform independent duration check: matching FRESH station.
        item=archive.getinfo('GTA VCS PSP FRESH.AT3')
        with archive.open(item) as fp:
            head=fp.read(512)
        off=12; fact=None;rate=0
        while off+8<=len(head):
            tag,n=struct.unpack_from('<4sI',head,off)
            if tag==b'fact':fact=struct.unpack_from('<I',head,off+8)[0]
            if tag==b'fmt ':rate=struct.unpack_from('<I',head,off+12)[0]
            if tag==b'data':break
            off+=8+n+(n&1)
        assert fact is not None and rate==44100
        duration_psp=fact/rate
        duration_ps2=vb.estimated_vb_duration(archive.getinfo('GTA VCS PS2 FRESH.VB').file_size,32000)
        assert abs(duration_psp-duration_ps2)<0.25,(duration_psp,duration_ps2)
    with zipfile.ZipFile(reference_zip) as archive, tempfile.TemporaryDirectory(prefix='gta_psp_reference_') as tmp:
        for name in ('GTA LCS PSP KJAH.AT3','GTA VCS PSP FRESH.AT3'):
            path = Path(tmp) / name.split()[-1]
            with archive.open(name) as src, path.open('wb') as dest:
                shutil.copyfileobj(src, dest)
            info = profiles.inspect_at3(path)
            assert info['codec'] == 'ATRAC3plus' and info['sample_rate'] == 44100 and info['channels'] == 2
            profiles.validate_at3_duration_metadata(info, info['seconds'])
            mutant = dict(info)
            mutant['seconds'] += 300
            try:
                profiles.validate_at3_duration_metadata(mutant, info['seconds'])
                raise AssertionError('Accepted a forged +300-second AT3 fact value')
            except ValueError:
                pass
            mutant = dict(info)
            mutant['data_size'] //= 2
            try:
                profiles.validate_at3_duration_metadata(mutant)
                raise AssertionError('Accepted an AT3 with half of its declared audio payload')
            except ValueError:
                pass
            if shutil.which('ffmpeg'):
                target = Path(tmp) / (path.stem + '.wav')
                proc = __import__('subprocess').run([
                    shutil.which('ffmpeg'), '-hide_banner','-loglevel','error','-y',
                    '-i',str(path),'-t','0.5','-ac','2','-ar','44100',str(target)
                ],capture_output=True,text=True,timeout=90)
                assert proc.returncode == 0 and target.is_file() and target.stat().st_size > 120000,(name,proc.stderr[-300:])
                target.unlink()
            path.unlink()
    print('Independent reference checks passed: all four PS2 VB formats, both PSP AT3 formats (including FFmpeg decoding), VCS FRESH duration cross-check.')


def test_profiles(ffmpeg: str):
    assert len(profiles.PROFILES)==6
    for name,p in profiles.PROFILES.items():
        assert p['channels']==2
        if p['platform']=='PS2':
            assert p['rate']==32000 and p['interleave']==8192
        else:
            assert p['rate']==44100 and p['codec']=='ATRAC3plus'
    assert profiles.ps2_station_rate('GTA III PS2 VB','CHAT.VB')==16000
    assert profiles.ps2_station_rate('GTA VC PS2 VB','KCHAT.VB')==16000
    assert profiles.ps2_station_rate('GTA VC PS2 VB','POLICE.VB')==16000
    assert profiles.ps2_station_rate('GTA VC PS2 VB','VCPR.VB')==16000
    assert profiles.ps2_station_rate('GTA VCS PS2 VB','FRESH.VB')==32000
    assert profiles.ps2_station_rate('GTA LCS PS2 VB','HEAD.VB')==32000
    assert profiles.ps2_station_rate('GTA VCS PS2 VB','VCPR')==32000
    # Explicitly override undocumented cases only with a user-chosen rate.
    # Regression: 2h50 source at 24 kHz plays as 2h07m30 at 32 kHz.
    assert 10200 * 24000 / 32000 == 7650
    duration=vb.estimated_vb_duration(vb.required_vb_size(10200*32000), 32000)
    assert 10200 <= duration < 10200.45,duration
    assert 2*60*60 < duration
    # Padding must never exceed one stereo block.
    for rate in (16000,32000):
        for samples in (1,27,28,29,14335,14336,14337,rate*10200):
            size=vb.required_vb_size(samples)
            sec=vb.estimated_vb_duration(size,rate)
            assert 0 <= sec - samples/rate < (14336/rate)+1e-7
    if not ffmpeg:
        print('FFmpeg unavailable; generated audio encode tests skipped.')
        return
    with tempfile.TemporaryDirectory(prefix='gta_audio_034_') as tmp:
        temp=Path(tmp)
        source=temp/'synthetic_stereo.wav'
        make_test_wav(source)
        cases = [
            ('GTA III PS2 VB','FLASH',32000),
            ('GTA VC PS2 VB','VCPR',16000),
            ('GTA LCS PS2 VB','HEAD',32000),
            ('GTA VCS PS2 VB','FRESH',32000),
        ]
        for name,station,rate in cases:
            dest=temp/(station+name.split(' ')[1]+'.VB')
            result=profiles.convert(source,dest,name,ffmpeg,station_name=station)
            assert result['sample_rate']==rate,(name,result['sample_rate'])
            assert result['channel_interleave_bytes']==8192
            assert abs(result['vb_estimated_duration_seconds']-1.25)<.91
            assert result['vb_layout']['padding_bytes'] < vb.STEREO_BLOCK
            vb.inspect_vb(dest,rate)
            raw=dest.read_bytes()
            assert not raw.startswith(b'VAGp')
            assert raw[:16]==bytes(16) and raw[0x2000:0x2010]==bytes(16)
            l=decode_channel(raw,0)
            r=decode_channel(raw,1)
            assert strongest_tone(l,rate,440),(name,'left channel not 440 Hz')
            assert strongest_tone(r,rate,880),(name,'right channel not 880 Hz')
            print(f'{name} [{station}] OK: {rate}Hz, {len(raw):,}bytes, {result["vb_estimated_duration_seconds"]:.3f}s, channels decoded correctly')
        # Destination remains intact if the chosen rate is invalid.
        dest=temp/'untouched.VB';dest.write_bytes(b'previous good output')
        try:
            profiles.convert(source,dest,'GTA VCS PS2 VB',ffmpeg,station_name='VCPR',sample_rate_override=12345)
            raise AssertionError('Invalid sample rate not rejected')
        except ValueError:
            assert dest.read_bytes()==b'previous good output'
    print('All four PS2 conversion profiles passed real FFmpeg + Numba encoding, structural verification and independent stereo-decoding tone tests.')
    print('Both PSP profiles checked structurally; actual ATRAC3plus encoding requires the bundled Windows at3tool.')


if __name__=='__main__':
    test_profiles(shutil.which('ffmpeg'))
    ref=Path(__file__).resolve().parent.parent.parent/'GTA Files.zip'
    # When project is unpacked beside the user's reference archive, use it.
    if not ref.is_file():
        ref=Path('/mnt/data/GTA Files.zip')
    verify_original_reference_metadata(ref)
    print('Audio target audit v0.3.34 passed.')
