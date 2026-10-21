"""Native +dB radio regressions, including physical container invariants."""
from __future__ import annotations
import math, os, struct, tempfile, wave
from pathlib import Path
import PS2_Audio_Encoder as au
import PS2_VB_Radio as vb
import Radio_Volume_Boost as gain


def _wav(path, rate, seconds=0.8, freq=400.0):
    with wave.open(str(path),'wb') as wf:
        wf.setnchannels(2); wf.setsampwidth(2); wf.setframerate(rate)
        samples = int(seconds * rate)
        pcm = bytearray()
        for i in range(samples):
            v=int(4000 * math.sin(i * 2 * math.pi * freq / rate))
            pcm.extend(struct.pack('<hh',v,v))
        wf.writeframes(pcm)


def _decoded_mono(frame: bytes, dec1=0, dec2=0):
    if not frame or len(frame)%16: raise ValueError('need frames')
    fs=((0,0),(60,0),(115,-52),(98,-55),(122,-60))
    pcm=[]
    for off in range(0,len(frame),16):
        filter_shift=frame[off]; p=filter_shift>>4; s=filter_shift&15
        assert p<5 and s<=12
        c1,c2=fs[p]
        for i in range(28):
            b=frame[off+2+i//2]; q=(b&15) if i%2==0 else b>>4
            if q>=8:q-=16
            v=((dec1*c1+dec2*c2+32)>>6)+q*(1<<(12-s))
            v=max(-32768,min(32767,v))
            dec2,dec1=dec1,v
            pcm.append(v)
    return pcm


def _rms(values):
    return math.sqrt(sum(v*v for v in values)/len(values))


def test_vb():
    with tempfile.TemporaryDirectory() as td:
        d=Path(td); pcm=d/'test.wav';raw=d/'raw.adpcm';source=d/'original.VB';dst=d/'boosted.VB'
        _wav(pcm,32000)
        meta=au.encode_pcm_wav_to_ps2_payload(pcm,raw,32000)
        vb.repack_stereo_frames(raw,source)
        result=gain.boost_vb(source,dst,3.0)
        assert result['bytes']==source.stat().st_size==dst.stat().st_size
        assert source.read_bytes()!=dst.read_bytes()
        vb.inspect_vb(dst,32000)
        old=source.read_bytes();new=dst.read_bytes()
        assert old[:16]==new[:16]==bytes(16)
        pre=_decoded_mono(old[0x100:0x100+16*120])
        post=_decoded_mono(new[0x100:0x100+16*120])
        # Decoding a middle-range section without its predecessor history is an
        # approximation; use the first channel from its known starting state.
        pre=_decoded_mono(old[:16*130]);post=_decoded_mono(new[:16*130])
        ratio=_rms(post[100:])/max(1,_rms(pre[100:]))
        assert 1.24 < ratio < 1.58, ratio
        try: gain.boost_vb(source,source,3)
        except ValueError: pass
        else: raise AssertionError('overwriting original VB was allowed')
        try: gain.boost_vb(source,dst,3)
        except FileExistsError: pass
        else: raise AssertionError('overwriting output VB was allowed')
        assert gain.validated_gain(3.0)==3.0
        for bad in (0, -1, 13, float('nan')):
            try: gain.validated_gain(bad)
            except ValueError: pass
            else: raise AssertionError('accepted out-of-range gain')


def test_gtasa_type4():
    with tempfile.TemporaryDirectory() as td:
        d=Path(td);full_wav=d/'full.wav';mini_wav=d/'mini.wav'
        _wav(full_wav,24000,0.75,400)
        _wav(mini_wav,750,0.75,100)
        full=d/'full.adpcm';mini=d/'mini.adpcm';pakentry=d/'entry.bin';dst=d/'boosted.bin'
        au.encode_pcm_wav_to_ps2_payload(full_wav,full,24000)
        au.encode_pcm_wav_to_ps2_payload(mini_wav,mini,750)
        meta=au.pack_gtasa_ps2_stream_payload(full,mini,pakentry)
        header=bytearray(0x1F84);header[:4]=b'\xff'*4
        struct.pack_into('<I',header,0x1F80,4)
        header=au.update_gtasa_type4_stream_header(header,meta,24000)
        au.validate_gtasa_type4_stream(header,pakentry)
        result=gain.boost_gtasa_type4(pakentry,header,dst,3)
        assert result['bytes']==dst.stat().st_size==pakentry.stat().st_size
        assert dst.read_bytes()!=pakentry.read_bytes()
        au.validate_gtasa_type4_stream(header,dst)
        before=pakentry.read_bytes();after=dst.read_bytes()
        full_bytes=meta['full_channel_bytes']
        mini_bytes=meta['mini_channel_bytes']
        # All four encoded streams receive volume change; blank padding stays identical.
        for off,n in ((0,mini_bytes),(0x800,mini_bytes),(0x1000,full_bytes),(0x11000,full_bytes)):
            assert before[off:off+n]!=after[off:off+n]
        assert before[mini_bytes:0x800]==after[mini_bytes:0x800]
        assert before[0x1000+full_bytes:0x11000]==after[0x1000+full_bytes:0x11000]


def test_source_encoding_gain():
    """The UI's PCM-before-encode path must produce measurably louder audio."""
    import shutil
    ffmpeg=shutil.which('ffmpeg')
    if not ffmpeg:
        print('Skipped FFmpeg gain test: ffmpeg unavailable')
        return
    with tempfile.TemporaryDirectory() as td:
        d=Path(td); source=d/'source.wav'
        _wav(source,32000,0.85,440)
        baseline=d/'no_gain.adpcm';raised=d/'plus3db.adpcm'
        x=au.convert_audio_to_ps2_payload(source,baseline,ffmpeg,32000)
        y=au.convert_audio_to_ps2_payload(source,raised,ffmpeg,32000,gain_db=3.0)
        assert x['gain_db']==0.0 and y['gain_db']==3.0
        assert x['pcm_frames']==y['pcm_frames']
        assert baseline.stat().st_size==raised.stat().st_size
        def left_frames(path):
            data=path.read_bytes();return b''.join(data[i:i+16] for i in range(0,len(data),32))
        original=_decoded_mono(left_frames(baseline))
        boosted=_decoded_mono(left_frames(raised))
        ratio=_rms(boosted[100:])/_rms(original[100:])
        assert 1.30 < ratio < 1.53, f'Expected about +3dB (1.412x), got {ratio}'
        for invalid in (-2.0,14.0,float('inf')):
            try:au.convert_audio_to_ps2_payload(source,d/'invalid.adpcm',ffmpeg,32000,gain_db=invalid)
            except au.AudioEncodeError: pass
            else:raise AssertionError(f'Invalid {invalid} dB gain accepted')



if __name__=='__main__':
    test_vb();print('PS2 VB volume test passed')
    test_gtasa_type4();print('GTA SA PAK type-4 volume test passed')
    test_source_encoding_gain();print('PS2 PCM-before-ADPCM gain test passed')
