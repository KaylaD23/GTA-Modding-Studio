"""GUI smoke/integration tests; run on Linux with xvfb-run, or Windows desktop.

Do not open dialogs or replace real game files. All assets are synthetic.
"""
import json
import os
import struct
import tempfile
import time
from pathlib import Path

from tkinter import filedialog,messagebox
import PS2_Audio_Encoder as audio
import PS2_VB_Radio as vb
import Test_Radio_Volume_Boost as tests
from GTA_Modding_Studio import Studio


def until_complete(app, timeout=40):
    end=time.monotonic()+timeout
    while app.busy and time.monotonic()<end:
        app.update()
        time.sleep(0.04)
    app.update()
    assert not app.busy, 'GUI worker did not finish within test timeout'


def run():
    errors=[]
    with tempfile.TemporaryDirectory() as folder:
        d=Path(folder)
        original=d/'source.wav';raw=d/'raw.adpcm';vbinput=d/'VCPR.VB';outvb=d/'VCPR_3dB.VB'
        tests._wav(original,32000,0.8)
        audio.encode_pcm_wav_to_ps2_payload(original,raw,32000)
        vb.repack_stereo_frames(raw,vbinput)

        askopen_backup=filedialog.askopenfilename
        asksave_backup=filedialog.asksaveasfilename
        askyesno_backup=messagebox.askyesno
        showinfo_backup=messagebox.showinfo
        showerror_backup=messagebox.showerror
        showwarning_backup=messagebox.showwarning
        filedialog.askopenfilename=lambda **kwargs:str(vbinput)
        filedialog.asksaveasfilename=lambda **kwargs:str(outvb)
        messagebox.askyesno=lambda *a,**kw:True
        messagebox.showinfo=lambda *a,**kw:None
        messagebox.showwarning=lambda *a,**kw:None
        messagebox.showerror=lambda *a,**kw:errors.append((a,kw))
        app=None
        try:
            app=Studio()
            app.gain_db.set('3.0')
            assert app._conversion_gain(app.radio_gain_enabled)==0.0
            app.radio_gain_enabled.set(True)
            assert app._conversion_gain(app.radio_gain_enabled)==3.0
            app.boost_vb_from_audio_tab()
            assert app.busy
            until_complete(app)
            assert outvb.is_file() and not errors,(outvb,errors)
            assert outvb.stat().st_size==vbinput.stat().st_size
            # Current default profile is SA, so boosted VB must not secretly
            # become the selected direct radio asset for the wrong game.
            assert not app.direct_radio_source.get()
            # Prepare a synthetic SA type-4 extracted track and matching manifest.
            full_wav=d/'full.wav';mini_wav=d/'mini.wav'
            tests._wav(full_wav,24000,0.75)
            tests._wav(mini_wav,750,0.75,100)
            full=d/'full.adpcm';mini=d/'mini.adpcm'
            audio.encode_pcm_wav_to_ps2_payload(full_wav,full,24000)
            audio.encode_pcm_wav_to_ps2_payload(mini_wav,mini,750)
            tracks=d/'tracks';tracks.mkdir()
            entry=tracks/'entry_0042.bin';hdrpath=tracks/'entry_0042.hdr'
            meta=audio.pack_gtasa_ps2_stream_payload(full,mini,entry)
            header=bytearray(0x1F84);header[:4]=b'\xff'*4
            struct.pack_into('<I',header,0x1F80,4)
            hdrpath.write_bytes(audio.update_gtasa_type4_stream_header(header,meta,24000))
            original_pak=d/'CH.PAK';original_lookup=d/'TRAKLKUP.DAT'
            original_pak.write_bytes(b'test');original_lookup.write_bytes(b'test')
            (tracks/'manifest.json').write_text(json.dumps({'source_pak':str(original_pak.resolve()),
                'source_lookup':str(original_lookup.resolve()),'tracks':[{'table_record':42,'has_stream_header':True}]}))
            app.pak.set(str(original_pak));app.lookup.set(str(original_lookup))
            app.extract_dir.set(str(tracks))
            old=entry.read_bytes()
            app.boost_prepared_radio()
            assert app.busy
            until_complete(app)
            assert entry.read_bytes()!=old and not errors, errors
            audio.validate_gtasa_type4_stream(hdrpath.read_bytes(),entry)
            assert original_pak.read_bytes()==b'test'
        finally:
            if app:app.destroy()
            filedialog.askopenfilename=askopen_backup
            filedialog.asksaveasfilename=asksave_backup
            messagebox.askyesno=askyesno_backup
            messagebox.showinfo=showinfo_backup
            messagebox.showerror=showerror_backup
            messagebox.showwarning=showwarning_backup
    print('GUI VB +3dB and extracted PAK +3dB button integration passed')


if __name__=='__main__':
    run()
