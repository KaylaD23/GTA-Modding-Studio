"""Six explicitly selected GTA PS2/PSP radio preparation targets.

PS2 radio VB uses 32 kHz stereo PSX/SPU ADPCM with 0x2000-byte channel
interleave in normal stations. GTA III CHAT and GTA Vice City KCHAT/POLICE/VCPR
are 16 kHz exceptions. Raw VB files do not contain a sample-rate header.
These files are distinct from GTA San Andreas PAK audio stream layouts.
PSP AT3 needs a separate ATRAC3plus encoder; FFmpeg does not provide one.
This module will not silently rename PCM/MP3 into AT3.
"""
from __future__ import annotations
from pathlib import Path
import os, shutil, subprocess, struct
import PS2_Audio_Encoder as ps2
import PS2_VB_Radio as vb
import tempfile, wave

# The PSP Stories streams use ATRAC3plus (not the older ATRAC3 GUID).
# Both GUIDs are stored little-endian in WAVEFORMATEXTENSIBLE.SubFormat.
ATRAC3_SUBTYPE = bytes.fromhex('7002000000001000800000aa00389b71')
ATRAC3PLUS_SUBTYPE = bytes.fromhex('bfaa23e958cb7144a119fffa01e4ce62')

PROFILES = {
    'GTA III PS2 VB':dict(platform='PS2',ext='.VB',rate=32000,interleave=0x2000,channels=2,codec='SPU ADPCM',status='0x2000 channel interleave; 32 kHz normally, 16 kHz for documented talk stations'),
    'GTA VC PS2 VB':dict(platform='PS2',ext='.VB',rate=32000,interleave=0x2000,channels=2,codec='SPU ADPCM',status='0x2000 channel interleave; 32 kHz normally, 16 kHz for documented talk stations'),
    'GTA LCS PS2 VB':dict(platform='PS2',ext='.VB',rate=32000,interleave=0x2000,channels=2,codec='SPU ADPCM',status='0x2000 channel interleave; 32 kHz normally, 16 kHz for documented talk stations'),
    'GTA VCS PS2 VB':dict(platform='PS2',ext='.VB',rate=32000,interleave=0x2000,channels=2,codec='SPU ADPCM',status='0x2000 channel interleave; 32 kHz normally, 16 kHz for documented talk stations'),
    'GTA LCS PSP AT3':dict(platform='PSP',ext='.AT3',rate=44100,channels=2,codec='ATRAC3plus',status='uses bundled at3tool; compare against an original game AT3'),
    'GTA VCS PSP AT3':dict(platform='PSP',ext='.AT3',rate=44100,channels=2,codec='ATRAC3plus',status='uses bundled at3tool; compare against an original game AT3'),
}


def find_at3tool(preferred: str='', app_root:Path|None=None):
    # Bundled from the user's GTA Files.7z/at3tool folder, including msvcr71.dll.
    root = Path(app_root or Path(__file__).resolve().parent)
    roots = [root/'tools'/'at3tool'/'at3tool.exe', root/'Tools'/'at3tool'/'at3tool.exe',
             root/'at3tool.exe']
    if preferred:
        roots.insert(0,Path(preferred))
    for candidate in roots:
        if candidate.is_file():
            return str(candidate.resolve())
    return shutil.which('at3tool') or shutil.which('psp_at3tool')


def inspect_at3(path: Path) -> dict:
    """Validate RIFF chunk bounds and the actual ATRAC codec descriptor.

    Read chunk headers by seeking, rather than assuming all metadata fits in
    the first 64 KiB (large JUNK/LIST chunks are legal RIFF).
    """
    p = Path(path)
    size = p.stat().st_size
    if size < 12:
        raise ValueError('AT3 file is too small for a RIFF header.')
    with p.open('rb') as f:
        header = f.read(12)
        if header[:4] != b'RIFF' or header[8:] != b'WAVE':
            raise ValueError('AT3 output is not RIFF/WAVE.')
        total = struct.unpack_from('<I', header, 4)[0] + 8
        if total != size:
            raise ValueError(f'AT3 RIFF size {total} does not match file size {size}.')
        off = 12
        fmt = None
        fact = None
        payload = None
        data_offset = None
        while off + 8 <= size:
            f.seek(off)
            chunk_header = f.read(8)
            tag, n = struct.unpack('<4sI', chunk_header)
            end = off + 8 + n
            padded_end = end + (n & 1)
            if end > size or padded_end > size:
                raise ValueError('AT3 chunk extends past EOF.')
            if tag == b'fmt ':
                if fmt is not None or n < 16 or n > 4096:
                    raise ValueError('Duplicate, truncated or oversized AT3 fmt chunk.')
                fmt = f.read(n)
            elif tag == b'fact':
                if fact is not None or n < 4:
                    raise ValueError('Duplicate or truncated AT3 fact chunk.')
                fact = struct.unpack('<I', f.read(4))[0]
            elif tag == b'data':
                if payload is not None:
                    raise ValueError('Duplicate AT3 data chunk.')
                payload = n
                data_offset = off + 8
            off = padded_end
        if off != size:
            raise ValueError('AT3 has trailing bytes outside RIFF chunks.')
    if fmt is None or payload is None or fact is None:
        raise ValueError('AT3 missing valid fmt, fact or data chunk.')
    code, ch, rate = struct.unpack_from('<HHI', fmt)
    if ch not in (1, 2) or rate < 8000 or fact <= 0:
        raise ValueError('AT3 has invalid channels/rate/sample count.')
    if code not in (0x0270, 0xfffe):
        raise ValueError(f'AT3 format tag 0x{code:04X} is not a recognized ATRAC codec.')
    codec = 'ATRAC3'
    if code == 0xfffe:
        if len(fmt) < 40 or struct.unpack_from('<H', fmt, 16)[0] < 22:
            raise ValueError('Truncated WAVEFORMATEXTENSIBLE codec descriptor.')
        subtype = fmt[24:40]
        if subtype == ATRAC3PLUS_SUBTYPE:
            codec = 'ATRAC3plus'
        elif subtype == ATRAC3_SUBTYPE:
            codec = 'ATRAC3'
        else:
            raise ValueError('WAVEFORMATEXTENSIBLE subtype is not ATRAC3 or ATRAC3plus.')
    if payload == 0:
        raise ValueError('AT3 data chunk is empty.')
    return dict(format_tag=code, codec=codec, channels=ch, sample_rate=rate,
                samples=fact, seconds=fact/rate, fmt=fmt, bytes=size,
                data_size=payload, data_offset=data_offset)


# Confirmed 16 kHz exceptions in the original GTA III / Vice City PS2 music
# archives. All other station presets use 32 kHz (including Stories PS2).
LOW_RATE_STATIONS = {
    'GTA III PS2 VB': frozenset(('CHAT',)),
    'GTA VC PS2 VB': frozenset(('KCHAT', 'POLICE', 'VCPR')),
    'GTA LCS PS2 VB': frozenset(),
    'GTA VCS PS2 VB': frozenset(),
}


def validate_at3_duration_metadata(info: dict, expected_seconds: float | None = None) -> None:
    """Reject ATRAC3plus RIFF files whose fact/size metadata is inconsistent.

    Checks advertised sample count against the encoded data's nominal byte
    rate, and (when provided) the source PCM duration. Decoder/runtime checks
    remain separate from these structural constraints.
    """
    if info.get('codec') != 'ATRAC3plus':
        raise ValueError('PSP LCS/VCS require ATRAC3plus, not another ATRAC codec.')
    seconds = float(info['seconds'])
    if expected_seconds is not None and abs(seconds - expected_seconds) > 2.0:
        raise ValueError(f'ATRAC3plus duration mismatch: input {expected_seconds:.3f}s, encoded fact {seconds:.3f}s.')
    average_bytes_per_sec = struct.unpack_from('<I', info['fmt'], 8)[0]
    if average_bytes_per_sec <= 0:
        raise ValueError('ATRAC3plus output has an invalid average byte rate.')
    payload_estimate = info['data_size'] / average_bytes_per_sec
    if abs(payload_estimate - seconds) > max(3.0, 0.005 * seconds):
        raise ValueError(f'ATRAC3plus data size does not agree with its declared duration (payload {payload_estimate:.2f}s versus fact {seconds:.2f}s).')


def ps2_station_rate(profile_name: str, station_name: str = '') -> int:
    """Return the correct playback sample rate for a documented PS2 station."""
    if profile_name not in LOW_RATE_STATIONS:
        raise ValueError('This is not a PS2 VB conversion profile.')
    station = Path(station_name.strip()).stem.upper()
    if not station or station in ('AUTO', 'STANDARD'):
        return PROFILES[profile_name]['rate']
    if station in LOW_RATE_STATIONS[profile_name]:
        return 16000
    # A station with the same name in another game does not necessarily have
    # the same rate (e.g. VCPR exists in the Stories games too).
    # Do not reject such names or borrow another game's 16 kHz exception.
    return 32000


def normalize_vb_frames(path: Path) -> dict:
    """Deprecated name for validation of an already correctly interleaved VB.

    GTA SA's alternating-frame ADPCM must be converted by
    vb.repack_stereo_frames BEFORE calling this function.
    """
    return vb.inspect_vb(path, 32000)


def convert(source:Path, output:Path, profile_name:str, ffmpeg:str, at3tool:str='', reference_at3:str='', progress=None, station_name:str='', sample_rate_override:int|None=None)->dict:
    if profile_name not in PROFILES:raise ValueError('Choose one of the six supported GTA targets.')
    p=PROFILES[profile_name]; source=Path(source);output=Path(output)
    if output.resolve()==source.resolve():raise ValueError('Never overwrite the source audio file.')
    output.parent.mkdir(parents=True,exist_ok=True)
    if p['platform']=='PS2':
        rate = ps2_station_rate(profile_name, station_name)
        if sample_rate_override is not None:
            if sample_rate_override not in (16000, 32000):
                raise ValueError('PS2 radio playback rate must be 16000 or 32000 Hz.')
            rate = sample_rate_override
        # Keep a previous destination intact until both encoding and GTA-style
        # 0x2000-byte channel interleave have passed full validation.
        with tempfile.TemporaryDirectory(prefix='.gta_vb_', dir=str(output.parent)) as temp:
            stage = Path(temp)
            raw = stage / 'stereo_frame_pairs.adpcm'
            packed = stage / 'radio.VB'
            meta = ps2.convert_audio_to_ps2_payload(source, raw, ffmpeg, rate, progress=progress)
            layout = vb.repack_stereo_frames(raw, packed)
            verified = vb.inspect_vb(packed, rate)
            if packed.stat().st_size != vb.required_vb_size(meta['pcm_frames']):
                raise ValueError('VB output size does not match the encoded sample count.')
            duration_error = verified['estimated_seconds'] - meta['duration_seconds']
            if duration_error < -0.000001 or duration_error > (vb.PAIRS_PER_BLOCK * 28 / rate) + 0.000001:
                raise ValueError('VB playback duration disagrees with the original PCM sample count.')
            os.replace(packed, output)
        return {'profile':profile_name,'output':str(output),'size':output.stat().st_size,
                'codec':p['codec'],'sample_rate':rate,'channel_interleave_bytes':vb.CHANNEL_BLOCK,
                'source_duration_seconds':meta['duration_seconds'],
                'vb_estimated_duration_seconds':verified['estimated_seconds'],
                'padding_duration_seconds':duration_error,
                'encoded_channels_identical':layout['encoded_channels_identical'],
                'compatibility':p['status'] + '; requires game/emulator playback validation',
                'encoder_metadata':meta,'vb_layout':layout,'vb_verified':verified}
    tool=find_at3tool(at3tool)
    if not tool:raise ValueError('Bundled PSP ATRAC3plus encoder missing. Restore tools/at3tool/at3tool.exe (and msvcr71.dll) from the release, or browse to another compatible tool.')
    with __import__('tempfile').TemporaryDirectory(prefix='gta_psp_at3_') as t:
        wav=Path(t)/'normalized.wav';dest=Path(t)/'encoded.at3'
        proc=subprocess.run([ffmpeg,'-hide_banner','-loglevel','error','-y','-i',str(source),'-map','0:a:0','-vn','-ar','44100','-ac','2','-c:a','pcm_s16le',str(wav)],capture_output=True,text=True,timeout=3600)
        if proc.returncode or not wav.is_file():raise RuntimeError('FFmpeg PCM conversion failed: '+proc.stderr[-600:])
        # The bundled Sony utility uses -e -br N. Supplied LCS/VCS PSP
        # originals are approximately 96 kbps (12,058 bytes/sec, 44.1 kHz).
        # Derive that target from the supplied original if one is selected.
        ref_info = inspect_at3(Path(reference_at3)) if reference_at3 else None
        bitrate_kbps = round(struct.unpack_from('<I', ref_info['fmt'], 8)[0] * 8 / 1000) if ref_info else 96
        proc=subprocess.run([tool,'-e','-br',str(bitrate_kbps),str(wav),str(dest)],capture_output=True,text=True,timeout=3600)
        if proc.returncode or not dest.exists():raise RuntimeError('ATRAC3plus encoder failed: '+(proc.stdout+' '+proc.stderr)[-1000:])
        out_info=inspect_at3(dest)
        with wave.open(str(wav), 'rb') as normalized:
            pcm_duration = normalized.getnframes() / normalized.getframerate()
        validate_at3_duration_metadata(out_info, pcm_duration)
        if ref_info:
            if ref_info['codec'] != 'ATRAC3plus':
                raise ValueError('Selected original reference is not ATRAC3plus; check the original game/platform.')
            if out_info['format_tag']!=ref_info['format_tag'] or out_info['channels']!=ref_info['channels'] or out_info['sample_rate']!=ref_info['sample_rate'] or out_info['fmt']!=ref_info['fmt']:
                raise ValueError('Encoded AT3 fmt chunk does not match selected game original. This is not safe as a drop-in replacement.')
        # Stage in the destination directory and replace only after a complete
        # validated copy. A failed copy must not destroy an existing output.
        # tempfile and wave imported at module scope
        stage = None
        try:
            with tempfile.NamedTemporaryFile(prefix='.gta_at3_', suffix='.tmp',
                                             dir=output.parent, delete=False) as handle:
                stage = Path(handle.name)
                with dest.open('rb') as encoded:
                    shutil.copyfileobj(encoded, handle)
            inspect_at3(stage)
            os.replace(stage, output)
        finally:
            if stage is not None:
                stage.unlink(missing_ok=True)
    return {'profile':profile_name,'output':str(output),'size':output.stat().st_size,'codec':'ATRAC3plus','duration_seconds':out_info['seconds'],'matching_original':bool(ref_info),'compatibility':'Structural AT3 verified; game loader/cache header must also be updated by ISO patcher. Emulator test required.'}


def run_self_test():
    assert len(PROFILES)==6
    assert list(PROFILES)==['GTA III PS2 VB','GTA VC PS2 VB','GTA LCS PS2 VB','GTA VCS PS2 VB','GTA LCS PSP AT3','GTA VCS PSP AT3']
    for k,v in PROFILES.items():
        assert v['ext'] in ('.VB','.AT3') and v['channels']==2
        if v['platform']=='PS2': assert v['rate']==32000 and v['interleave']==0x2000
    assert ps2_station_rate('GTA III PS2 VB', 'CHAT.VB')==16000
    assert ps2_station_rate('GTA VC PS2 VB', 'VCPR.VB')==16000
    assert ps2_station_rate('GTA VCS PS2 VB', 'FRESH.VB')==32000
    assert ps2_station_rate('GTA VCS PS2 VB', 'VCPR.VB')==32000
    assert Path(find_at3tool()).name.lower() == 'at3tool.exe', 'Bundled PSP ATRAC3plus encoder not detected'
if __name__=='__main__':run_self_test();print('Audio preparation profile self-tests passed')
