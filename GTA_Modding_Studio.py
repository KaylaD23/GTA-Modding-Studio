#!/usr/bin/env python3
"""GTA Modding Studio — native GUI for GTA asset and radio workflows.

This GUI is written for the project. Reference applications are not launched or
bundled; reusable PS2 archive/audio algorithms live in separate Python modules.
"""
from __future__ import annotations
import os, sys, csv, zlib, struct, threading, queue, shutil, tempfile, subprocess, re, json
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import PS2_PAK_Core as core
import PS2_Audio_Encoder as audio
import PS2_ISO_Patcher as iso_patcher
import Executable_Patch_Core as executable_patch_core
import Game_Code_Analysis as game_code_analysis
import PSP_EBOOT_Lab
import TIM2_Converter as tim2_converter
import Audio_Preparation_Profiles as audio_profiles
from asset_inspector import parse_rw_chunks
from texture_ui import TextureTabMixin
from texture_actions import TextureActionsMixin
try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    _TkBase = TkinterDnD.Tk
    DND_AVAILABLE = True
except ImportError:
    _TkBase = tk.Tk
    DND_AVAILABLE = False

APP_TITLE = 'GTA Modding Studio v0.3.39 — Radio & Texture Workbench'
BG='#111827'; PANEL='#1f2937'; PANEL2='#263244'; FG='#e5edf7'; MUTED='#a8b5c7'; ACCENT='#3b82f6'; BORDER='#374151'

def human(n):
    n=float(n)
    for u in ('B','KB','MB','GB'):
        if n<1024 or u=='GB': return f'{int(n):,} B' if u=='B' else f'{n:.1f} {u}'
        n/=1024

def read_cstr(data, start, end, encoding):
    if start < 0 or start >= end or end > len(data): return ''
    unit=2 if encoding=='utf-16le' else 1
    stop=end
    if unit==2:
        for p in range(start, end-1, 2):
            if data[p:p+2]==b'\0\0': stop=p; break
        raw=data[start:stop]
    else:
        p=data.find(b'\0', start, end); stop=p if p>=0 else end; raw=data[start:stop]
    try: return raw.decode(encoding, errors='replace')
    except LookupError: return raw.decode('latin-1', errors='replace')

class GXTDocument:
    """Conservative parser/editor for classic TABL/TKEY/TDAT GXT variants.

    Replacements are written in place and must fit the original string slot;
    this preserves all offsets and unknown bytes in platform-specific files.
    """
    def __init__(self, path: Path):
        self.path=path; self.original=path.read_bytes(); self.data=bytearray(self.original)
        self.tabl=self.data.find(b'TABL'); self.tdat=self.data.find(b'TDAT')
        if self.tabl<0 or self.tdat<0: raise ValueError('Could not find both TABL and TDAT markers.')
        if self.tabl+8>len(self.data) or self.tdat+8>len(self.data): raise ValueError('Truncated GXT header.')
        self.tabl_len=struct.unpack_from('<I',self.data,self.tabl+4)[0]
        self.tdat_len=struct.unpack_from('<I',self.data,self.tdat+4)[0]
        self.tdat_start=self.tdat+8; self.tdat_end=min(len(self.data),self.tdat_start+self.tdat_len)
        if self.tdat_start>=self.tdat_end: raise ValueError('Invalid TDAT bounds.')
        # Infer common string encoding from the first non-empty TDAT segment.
        probe=self.data[self.tdat_start:min(self.tdat_end,self.tdat_start+256)]
        self.encoding='utf-16le' if len(probe)>8 and probe[1::2].count(0)>len(probe[1::2])//3 else 'cp1252'
        self.tables=[]; self.entries=[]; self._parse()
    def _parse(self):
        p=self.tabl+8; end=min(len(self.data),self.tabl+8+self.tabl_len)
        sa_style=(self.tabl>=4 and self.data[self.tabl-4:self.tabl]==b"\x04\x00\x08\x00")
        while p+12<=end:
            name=self.data[p:p+8].split(b"\0",1)[0].decode("ascii","replace").strip()
            table_off=struct.unpack_from("<I",self.data,p+8)[0]
            if not name and table_off==0: break
            # Main tables point directly at TKEY; later tables in VC/Stories/SA
            # point at an eight-byte table name followed by the TKEY chunk.
            if table_off+8<=len(self.data) and self.data[table_off:table_off+4]==b"TKEY":
                tkey=table_off; data_start=tkey+8
            elif table_off+16<=len(self.data) and self.data[table_off+8:table_off+12]==b"TKEY":
                tkey=table_off+8; data_start=tkey+8
            else:
                p+=12; continue
            key_len=struct.unpack_from("<I",self.data,tkey+4)[0]
            key_start=data_start; key_end=min(len(self.data),key_start+key_len)
            layout='offset_crc' if sa_style else 'offset_name'
            self.tables.append({'name':name or f'TABLE_{len(self.tables)}','offset':table_off,'tkey':tkey,'key_start':key_start,'key_end':key_end,'layout':layout})
            p+=12
        # GXT string offsets are relative to the TDAT payload start. Each table
        # has its own TKEY block; SA uses offset+CRC32 (8 bytes), while VC and
        # the Stories games use offset+8-byte key (12 bytes).
        for table in self.tables:
            s=table['key_start']; e=table['key_end']; step=8 if table['layout']=='offset_crc' else 12
            for p in range(s,e-step+1,step):
                off=struct.unpack_from('<I',self.data,p)[0]
                if table['layout']=='offset_crc':
                    key=struct.unpack_from('<I',self.data,p+4)[0]; label=f'{key:08X}'
                else:
                    label=self.data[p+4:p+12].split(b'\0',1)[0].decode('ascii','replace')
                absolute=self.tdat_start+off
                if absolute<self.tdat_start or absolute>=self.tdat_end: continue
                self.entries.append({'table':table['name'],'key':label,'offset':off,'absolute':absolute,'record':p,'layout':table['layout']})
        offsets=sorted(set(x['offset'] for x in self.entries))
        for item in self.entries:
            nxt=next((o for o in offsets if o>item['offset']),self.tdat_end-self.tdat_start)
            item['slot']=max(0,nxt-item['offset'])
            item['text']=read_cstr(self.data,item['absolute'],min(self.tdat_end,self.tdat_start+nxt),self.encoding)
    def replace(self, item, text):
        old=item['text']; enc=self.encoding
        try: raw=text.encode(enc)
        except UnicodeEncodeError as exc: raise ValueError(f'Text cannot be encoded as {enc}: {exc}')
        unit=2 if enc=='utf-16le' else 1
        raw+=b'\0'*unit
        if len(raw)>item['slot']:
            raise ValueError(f'Replacement needs {len(raw)} bytes, but this string slot has {item["slot"]} bytes. This safe editor does not shift GXT offsets.')
        start=item['absolute']; end=start+item['slot']
        self.data[start:end]=raw.ljust(item['slot'],b'\0')
        item['text']=text
    def save(self,path): Path(path).write_bytes(self.data)

class Studio(TextureActionsMixin, TextureTabMixin, _TkBase):
    def __init__(self):
        super().__init__(); self.title(APP_TITLE); self.geometry('1180x760'); self.minsize(1000,620); self.configure(bg=BG)
        self._style(); self.q=queue.Queue(); self.busy=False
        self.last_rebuild_pak=None; self.last_rebuild_lookup=None; self.last_prepared_root=None; self.last_prepared_game_id=None; self.last_prepared_station_id=None
        self.game_choice=tk.StringVar(value=iso_patcher.GAME_RADIO_PROFILES['gta_sa_ps2']['title']); self.station_choice=tk.StringVar(); self.direct_radio_source=tk.StringVar(); self.direct_target_text=tk.StringVar(); self.direct_format_text=tk.StringVar(); self.direct_notes_text=tk.StringVar()
        self.pak=tk.StringVar(); self.lookup=tk.StringVar(); self.group=tk.StringVar(value='Auto-detect'); self.offset_unit=tk.StringVar(value='Auto-detect'); self.size_unit=tk.StringVar(value='Auto-detect')
        self.extract_dir=tk.StringVar(); self.out_dir=tk.StringVar(); self.ffmpeg=tk.StringVar(value=audio.find_ffmpeg() or '')
        self.gxt_doc=None; self.gxt_path=None; self.gxt_visible=[]; self.txd_data=None; self.txd_chunks=[]; self.dtz_raw=None; self.dtz_path=None
        self._menu(); self._ui(); self.after(100,self._poll)
    def _style(self):
        s=ttk.Style(self); s.theme_use('clam')
        s.configure('.',background=BG,foreground=FG,font=('Segoe UI',10)); s.configure('TFrame',background=BG); s.configure('Panel.TFrame',background=PANEL)
        s.configure('TLabel',background=BG,foreground=FG); s.configure('Muted.TLabel',background=BG,foreground=MUTED)
        s.configure('Header.TLabel',font=('Segoe UI',20,'bold'),foreground='#ffffff'); s.configure('TButton',background=PANEL2,foreground=FG,padding=(9,6))
        s.map('TButton',background=[('active','#3a4b63'),('disabled','#263244')]); s.configure('Accent.TButton',background=ACCENT,foreground='white',font=('Segoe UI',10,'bold'))
        s.configure('TEntry',fieldbackground='#0b1220',foreground=FG,insertcolor='white')
        # Explicit state maps stop the clam theme from reverting readonly combo fields
        # to pale grey with low-contrast light text. Apply to every dropdown, including
        # the Audio Preparation target-format dropdown and radio archive selectors.
        s.configure('TCombobox',fieldbackground='#0b1220',background=PANEL2,foreground=FG,
                    arrowcolor=FG,bordercolor='#374151',lightcolor='#374151',darkcolor='#374151',padding=5)
        s.map('TCombobox',
              fieldbackground=[('disabled',PANEL),('readonly','#0b1220'),('focus','#0b1220'),('!disabled','#0b1220')],
              foreground=[('disabled',MUTED),('readonly',FG),('focus',FG),('!disabled',FG)],
              background=[('disabled',PANEL),('readonly',PANEL2),('active','#3a4b63'),('!disabled',PANEL2)],
              arrowcolor=[('disabled',MUTED),('!disabled',FG)])
        # Tk uses a classic Listbox for the dropdown popup; colour that list as well.
        for option, value in [
            ('*TCombobox*Listbox.background','#0b1220'),
            ('*TCombobox*Listbox.foreground',FG),
            ('*TCombobox*Listbox.selectBackground',ACCENT),
            ('*TCombobox*Listbox.selectForeground','#ffffff')]:
            self.option_add(option,value)
        s.configure('TNotebook',background=BG,borderwidth=0); s.configure('TNotebook.Tab',background=PANEL,foreground=MUTED,padding=(13,8)); s.map('TNotebook.Tab',background=[('selected',ACCENT)],foreground=[('selected','white')])
        s.configure('Treeview',background='#0b1220',fieldbackground='#0b1220',foreground=FG,rowheight=24); s.configure('Treeview.Heading',background=PANEL2,foreground='white',padding=6)
        s.configure('TLabelframe',background=PANEL,foreground=FG); s.configure('TLabelframe.Label',background=PANEL,foreground='#93c5fd')
        s.configure('Horizontal.TProgressbar',troughcolor='#0b1220',background=ACCENT)
    def _menu(self):
        m=tk.Menu(self,tearoff=False); f=tk.Menu(m,tearoff=False)
        f.add_command(label='Open radio PAK…',command=self.pick_pak)
        f.add_command(label='Open GXT…',command=self.open_gxt)
        f.add_command(label='Open TXD…',command=self.open_txd)
        f.add_command(label='Open DTZ…',command=self.open_dtz)
        f.add_separator()
        f.add_command(label='Rebuild radio archive…',command=self.rebuild_radio)
        f.add_command(label='Prepare Files for ISO Patching…',command=self.prepare_files_for_iso_patching)
        f.add_command(label='Patch ISO…',command=self.patch_iso)
        f.add_separator(); f.add_command(label='Exit',command=self.destroy)
        m.add_cascade(label='File',menu=f)
        tools=tk.Menu(m,tearoff=False)
        tools.add_command(label='PSP EBOOT Decrypt & Static Analysis…', command=self.open_psp_eboot_lab)
        tools.add_command(label='Verify Bundled LCS PS2 Length Trace…', command=self.verify_lcs_length_trace)
        tools.add_command(label='PNG → TIM2 Converter', command=self.open_tim2_converter)
        m.add_cascade(label='Tools', menu=tools)
        h=tk.Menu(m,tearoff=False); h.add_command(label='About',command=lambda:messagebox.showinfo('About',APP_TITLE+'\nGame executables are static reference inputs only; they are never launched by the app. PSPDecrypt is used only as an explicit user-requested analysis tool.')); m.add_cascade(label='Help',menu=h); self.config(menu=m)
    def open_psp_eboot_lab(self):
        """Open the bundled PSPDecrypt wrapper and static EBOOT analyzer."""
        try:
            PSP_EBOOT_Lab.open_window(self, app_dir=Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent)
        except Exception as exc:
            messagebox.showerror("PSP EBOOT Lab", f"Could not open the PSP EBOOT analysis window:\n{exc}", parent=self)

    def verify_lcs_length_trace(self):
        """Recheck documented LCS PS2 code locations against the exact bundled build."""
        try:
            result = game_code_analysis.verify_lcs_ps2_length_trace(Path(__file__).resolve().parent)
            details = "\n".join(f"{item['label']}: file offset 0x{item['offset']:X} — {item['status']}" for item in result["checks"])
            messagebox.showinfo("LCS PS2 Length Trace", f"Reference: {result['reference']}\nSHA-256 verified: {result['sha256']}\n\n{details}\n\nRuntime source conclusion: {result['runtime_origin_conclusion']}\n\nNo executable bytes were modified.", parent=self)
        except Exception as exc:
            messagebox.showerror("LCS PS2 Length Trace", str(exc), parent=self)

    def _ui(self):
        head=ttk.Frame(self,padding=(18,12,18,8)); head.pack(fill='x'); ttk.Label(head,text='GTA Modding Studio',style='Header.TLabel').pack(side='left'); ttk.Label(head,text='Native modding workbench · v0.3.39',style='Muted.TLabel').pack(side='left',padx=16)
        self.status=tk.StringVar(value='Ready — open a game asset or radio archive.'); ttk.Label(self,textvariable=self.status,anchor='w',padding=(12,5),style='Muted.TLabel').pack(side='bottom',fill='x')
        self.tabs=ttk.Notebook(self); self.tabs.pack(fill='both',expand=True,padx=14,pady=(0,10))
        self.radio=ttk.Frame(self.tabs,padding=12); self.audio_tab=ttk.Frame(self.tabs,padding=12); self.txd_tab=ttk.Frame(self.tabs,padding=12); self.gxt_tab=ttk.Frame(self.tabs,padding=12); self.dtz_tab=ttk.Frame(self.tabs,padding=12); self.tim2_tab=ttk.Frame(self.tabs,padding=12)
        for frame,title in [(self.radio,'GTA PS2/PSP Radio Installer'),(self.audio_tab,'Audio Preparation'),(self.tim2_tab,'PNG → TIM2 Converter'),(self.txd_tab,'TXD Editor'),(self.gxt_tab,'GXT Editor'),(self.dtz_tab,'DTZ Tools')]: self.tabs.add(frame,text=title)
        self._radio_ui(); self._audio_ui(); self._tim2_ui(); self._txd_ui(); self._gxt_ui(); self._dtz_ui()
    def _browse_row(self,parent,label,var,cmd):
        row=ttk.Frame(parent); row.pack(fill='x',pady=3); ttk.Label(row,text=label,width=17).pack(side='left'); ttk.Entry(row,textvariable=var).pack(side='left',fill='x',expand=True,padx=6); ttk.Button(row,text='Browse…',command=cmd).pack(side='left'); return row
    def _radio_ui(self):
        selector=ttk.LabelFrame(self.radio,text='Target game / platform',padding=8); selector.pack(fill='x',pady=(0,8))
        game_row=ttk.Frame(selector); game_row.pack(fill='x')
        ttk.Label(game_row,text='Game').pack(side='left',padx=(0,8))
        self.game_combo=ttk.Combobox(game_row,textvariable=self.game_choice,state='readonly',width=47,
            values=[profile['title'] for profile in iso_patcher.GAME_RADIO_PROFILES.values()])
        self.game_combo.pack(side='left',fill='x',expand=True)
        self.game_combo.bind('<<ComboboxSelected>>',self._on_game_choice_changed)
        station_row=ttk.Frame(selector); station_row.pack(fill='x',pady=(7,0))
        ttk.Label(station_row,text='Radio station').pack(side='left',padx=(0,8))
        self.station_combo=ttk.Combobox(station_row,textvariable=self.station_choice,state='readonly',width=36,values=[])
        self.station_combo.pack(side='left',padx=(0,12))
        self.station_combo.bind('<<ComboboxSelected>>',self._on_station_choice_changed)
        self.radio_context_label=tk.StringVar(value='GTA San Andreas PS2 uses a PAK + TRAKLKUP.DAT radio archive.')
        ttk.Label(station_row,textvariable=self.radio_context_label,style='Muted.TLabel',wraplength=620).pack(side='left',fill='x',expand=True)

        self.sa_controls=ttk.Frame(self.radio)
        self.sa_controls.pack(fill='both',expand=True)
        files=ttk.LabelFrame(self.sa_controls,text='GTA San Andreas PS2 source archive',padding=8); files.pack(fill='x')
        self._browse_row(files,'Radio PAK',self.pak,self.pick_pak); self._browse_row(files,'TRAKLKUP.DAT',self.lookup,self.pick_lookup)
        opts=ttk.Frame(files); opts.pack(fill='x',pady=4)
        for label,var,vals in [('Archive ID',self.group,['Auto-detect']+[f'0x{i:02X}' for i in range(256)]),('Offset unit',self.offset_unit,['Auto-detect','Bytes','2048-byte LBA sectors']),('Size unit',self.size_unit,['Auto-detect','Bytes','2048-byte LBA sectors'])]:
            ttk.Label(opts,text=label).pack(side='left',padx=(0,4)); ttk.Combobox(opts,textvariable=var,values=vals,width=19,state='readonly').pack(side='left',padx=(0,12))
        actions=ttk.Frame(self.sa_controls); actions.pack(fill='x',pady=7)
        # A single six-button pack row clipped the final button on smaller screens.
        # Use a weighted 3x2 grid so all primary archive actions remain visible.
        action_defs=[('Inspect Archive',self.inspect_radio,'Accent.TButton'),('Extract Tracks',self.extract_radio,'TButton'),('Patch ISO…',self.patch_iso,'Accent.TButton'),('Batch Replace Songs…',self.batch_replace_tracks,'Accent.TButton'),('Auto-Rename Files',self.auto_rename_tracks,'TButton'),('Choose Tracks Folder',self.choose_tracks,'TButton'),('Rebuild Archive',self.rebuild_radio,'Accent.TButton'),('Prepare Files for ISO Patching…',self.prepare_files_for_iso_patching,'Accent.TButton')]
        self.radio_action_buttons={}
        for col in range(4): actions.grid_columnconfigure(col,weight=1,uniform='radio_action')
        for i,(text,cmd,style) in enumerate(action_defs):
            btn=ttk.Button(actions,text=text,command=cmd,style=style)
            btn.grid(row=i//4,column=i%4,sticky='ew',padx=4,pady=3)
            self.radio_action_buttons[text]=btn
        self.rebuild_button=self.radio_action_buttons['Rebuild Archive']
        self.sa_patch_button=self.radio_action_buttons['Patch ISO…']
        batch_progress=ttk.Frame(self.sa_controls); batch_progress.pack(fill='x',pady=(0,4))
        self.radio_progress=ttk.Progressbar(batch_progress,mode='determinate',maximum=100)
        self.radio_progress.pack(side='left',fill='x',expand=True)
        self.radio_progress_pct=tk.StringVar(value='0%')
        ttk.Label(batch_progress,textvariable=self.radio_progress_pct,width=6,anchor='e').pack(side='left',padx=(7,0))
        self.radio_progress_text=tk.StringVar(value='Batch replacement converts source audio to the selected PS2 radio entries.')
        ttk.Label(self.sa_controls,textvariable=self.radio_progress_text,style='Muted.TLabel').pack(fill='x',anchor='w')
        self.radio_drop_label=ttk.Label(self.sa_controls,text=('Drop one or more replacement songs here to batch-convert and replace them' if DND_AVAILABLE else 'Use Batch Replace Songs… to choose replacement audio files'),anchor='center',padding=5,style='Muted.TLabel')
        self.radio_drop_label.pack(fill='x',pady=(2,3))
        self.radio_tree=ttk.Treeview(self.sa_controls,columns=('id','offset','size','kind','rate'),show='headings',selectmode='extended');
        for c,t,w in [('id','Lookup record',100),('offset','Offset',130),('size','Stored size',130),('kind','Format',150),('rate','Sample rate',120)]: self.radio_tree.heading(c,text=t); self.radio_tree.column(c,width=w,anchor='w')
        self.radio_tree.pack(fill='both',expand=True,pady=4)
        # Right-click actions make one-to-one batch mapping practical for large stations.
        # 'Select All Supported Songs' excludes raw/unknown records that the batch
        # replacement safety checks will reject; 'Select All Rows' remains available.
        self.radio_context_menu = tk.Menu(
            self, tearoff=False, bg=PANEL, fg=FG,
            activebackground=ACCENT, activeforeground='#ffffff',
            disabledforeground=MUTED, borderwidth=1, relief='solid'
        )
        self.radio_context_menu.add_command(label='Select All Rows', command=self.select_all_radio_rows)
        self.radio_context_menu.add_command(label='Select All Supported Songs', command=self.select_supported_radio_rows)
        self.radio_context_menu.add_separator()
        self.radio_context_menu.add_command(label='Invert Selection', command=self.invert_radio_selection)
        self.radio_context_menu.add_command(label='Clear Selection', command=self.clear_radio_selection)
        self.radio_tree.bind('<Button-3>', self._show_radio_context_menu)
        self.radio_tree.bind('<Control-Button-1>', self._show_radio_context_menu)
        self.radio_tree.bind('<Control-a>', self._radio_select_all_shortcut)
        self.radio_tree.bind('<Control-A>', self._radio_select_all_shortcut)
        if DND_AVAILABLE:
            for widget in (self.radio_drop_label, self.radio_tree):
                widget.drop_target_register(DND_FILES)
                widget.dnd_bind('<<Drop>>', self.drop_radio_audio)
        dirs=ttk.Frame(self.sa_controls); dirs.pack(fill='x'); self._browse_row(dirs,'Extracted tracks',self.extract_dir,self.choose_tracks); self._browse_row(dirs,'Output folder',self.out_dir,self.choose_output)
        ttk.Label(self.sa_controls,text='Rebuild creates NEW-* files. Prepare stages the correctly named PAK + lookup table; Patch ISO creates and verifies a separate patched image copy (ISO or CSO), leaving the source image untouched.',style='Muted.TLabel',wraplength=1050).pack(anchor='w',pady=(4,0))

        self.direct_controls=ttk.Frame(self.radio)
        direct_info=ttk.LabelFrame(self.direct_controls,text='Game-specific radio stream',padding=12)
        direct_info.pack(fill='x',pady=(0,8))
        ttk.Label(direct_info,text='Target in game image',width=20).grid(row=0,column=0,sticky='w',pady=4)
        ttk.Label(direct_info,textvariable=self.direct_target_text,anchor='w').grid(row=0,column=1,sticky='ew',pady=4)
        ttk.Label(direct_info,text='Required asset format',width=20).grid(row=1,column=0,sticky='w',pady=4)
        ttk.Label(direct_info,textvariable=self.direct_format_text,anchor='w').grid(row=1,column=1,sticky='ew',pady=4)
        ttk.Label(direct_info,textvariable=self.direct_notes_text,style='Muted.TLabel',wraplength=970,justify='left').grid(row=2,column=0,columnspan=2,sticky='ew',pady=(8,4))
        direct_info.grid_columnconfigure(1,weight=1)
        source_frame=ttk.LabelFrame(self.direct_controls,text='Prepared whole-station replacement',padding=10)
        source_frame.pack(fill='x',pady=(0,8))
        ttk.Label(source_frame,text='Compatible radio asset',width=20).pack(side='left')
        ttk.Entry(source_frame,textvariable=self.direct_radio_source).pack(side='left',fill='x',expand=True,padx=6)
        ttk.Button(source_frame,text='Browse…',command=self.pick_direct_radio_asset).pack(side='left')
        direct_actions=ttk.Frame(self.direct_controls); direct_actions.pack(fill='x',pady=7)
        self.direct_prepare_button=ttk.Button(direct_actions,text='Prepare Radio File for ISO Patching…',command=self.prepare_files_for_iso_patching,style='Accent.TButton')
        self.direct_prepare_button.pack(side='left',padx=(0,6))
        self.direct_patch_button=ttk.Button(direct_actions,text='Patch ISO…',command=self.patch_iso,style='Accent.TButton')
        self.direct_patch_button.pack(side='left')
        ttk.Label(self.direct_controls,text='This platform uses a single game-compatible radio stream file instead of the San Andreas PAK + TRAKLKUP.DAT batch workflow. Select a finished .VB (PS2) or .AT3 (PSP) station asset; this screen copies it byte-for-byte and does not re-encode it. The image patcher verifies the selected game/platform before replacing the target path.',style='Muted.TLabel',wraplength=1050,justify='left').pack(fill='x',anchor='w',pady=(8,0))

        self.research_controls=ttk.Frame(self.radio)
        research_box=ttk.LabelFrame(self.research_controls,text='Executable-informed radio layout research',padding=14)
        research_box.pack(fill='x',anchor='n')
        ttk.Label(research_box,text='This game has a different or not-yet-confirmed radio file layout.',style='Header.TLabel',wraplength=950).pack(anchor='w',pady=(0,8))
        self.research_note_text=tk.StringVar(value='')
        ttk.Label(research_box,textvariable=self.research_note_text,style='Muted.TLabel',wraplength=1000,justify='left').pack(fill='x',anchor='w')
        ttk.Label(research_box,text='For safety, Prepare Radio File and Patch ISO are disabled for this profile until the exact game-disc target and its related lookup/stream files are confirmed from an original disc. Other game profiles remain available.',wraplength=1000,justify='left').pack(fill='x',anchor='w',pady=(10,0))
        self.patch_iso_button=self.sa_patch_button
        self._apply_game_context()

    def _refresh_station_options(self):
        """Populate station choices from the selected executable-backed profile."""
        game_id,base=next(((gid,p) for gid,p in iso_patcher.GAME_RADIO_PROFILES.items()
                           if p['title']==self.game_choice.get()),('gta_sa_ps2',iso_patcher.GAME_RADIO_PROFILES['gta_sa_ps2']))
        stations=base.get('stations') or []
        if not stations:
            self.station_combo.configure(values=(base.get('station','Radio archive'),),state='disabled')
            self.station_choice.set(base.get('station','Radio archive'))
            return
        labels=[row['station'] for row in stations]
        current=self.station_choice.get()
        if current not in labels:
            default_id=base.get('default_station_id')
            default=next((row for row in stations if row['id']==default_id),stations[0])
            current=default['station']
        self.station_combo.configure(values=labels,state='readonly')
        self.station_choice.set(current)

    def _selected_game_profile(self):
        for game_id,base in iso_patcher.GAME_RADIO_PROFILES.items():
            if base['title'] == self.game_choice.get():
                station_id=None
                for row in base.get('stations',[]):
                    if row['station']==self.station_choice.get():
                        station_id=row['id']; break
                return game_id,iso_patcher.resolve_game_radio_profile(game_id,station_id)
        return 'gta_sa_ps2',iso_patcher.resolve_game_radio_profile('gta_sa_ps2')

    def _on_game_choice_changed(self,_event=None):
        self.direct_radio_source.set('')
        self._refresh_station_options()
        self._apply_game_context()

    def _on_station_choice_changed(self,_event=None):
        self._apply_game_context()

    def _apply_game_context(self):
        self._refresh_station_options()
        game_id,profile=self._selected_game_profile()
        key=(game_id,profile.get('station_id'),profile.get('target_path'))
        previous_key=getattr(self,'_last_direct_profile_key',None)
        if profile['mode']=='direct' and previous_key is not None and previous_key!=key:
            self.direct_radio_source.set('')
        self._last_direct_profile_key=key if profile['mode']=='direct' else None
        self.current_game_id=game_id
        # Always hide non-selected layouts first so the panel cannot overlap.
        self.sa_controls.pack_forget()
        self.direct_controls.pack_forget()
        self.research_controls.pack_forget()
        if profile['mode']=='sa_pak':
            self.sa_controls.pack(fill='both',expand=True)
            self.patch_iso_button=self.sa_patch_button
            self.radio_context_label.set('PS2 · PAK + TRAKLKUP.DAT · batch song conversion and archive rebuilding')
            if hasattr(self,'audio_context_help'): self.audio_context_help.set('GTA San Andreas PS2 uses the PAK + TRAKLKUP.DAT stream archive. Use Batch Replace Songs to create the required type-4 dual-stream payloads; generic ADPCM/VAG output is not a replacement for this format.')
            self.status.set('Selected GTA San Andreas PS2 radio archive mode.')
        elif profile['mode']=='research_required':
            self.research_controls.pack(fill='both',expand=True)
            self.patch_iso_button=None
            self.research_note_text.set(profile.get('research_note','The radio target for this game is not confirmed yet.'))
            self.radio_context_label.set(f"{profile['platform']} · radio target requires verification from original game data")
            if hasattr(self,'audio_context_help'): self.audio_context_help.set(f"{profile['title']}: the executable identifies a lookup/stream-based radio system, but the exact replacement asset has not yet been validated. Do not patch a guessed file path.")
            self.status.set(f"{profile['title']} needs radio target research; patching is disabled to protect the game image.")
        else:
            self.direct_controls.pack(fill='both',expand=True)
            self.patch_iso_button=self.direct_patch_button
            self.direct_target_text.set(profile['target_path'])
            self.direct_format_text.set(f"{profile['asset_extension'].upper()} — {profile['asset_description']}")
            notes=(f"Platform: {profile['platform']}   |   Radio station: {profile['station']}   |   Required file: {profile['asset_filename']}\n"
                   f"The Patch ISO action checks the image's {'PARAM.SFO' if profile['platform']=='PSP' else 'SYSTEM.CNF'} against known {profile['platform']} disc IDs, then patches only the target shown above. Use an already game-compatible, whole-station stream file.")
            if profile.get('header_companion_path'):
                slot_offset=int(profile.get('header_slot_index',0))*int(profile.get('header_block_size',0x800))
                notes+=(f"\nThe patcher also updates slot {profile['header_slot_index']} (offset 0x{slot_offset:X}, size 0x{int(profile.get('header_block_size',0x800)):X}) in {profile['header_companion_path']} using the replacement stream's first 0x800 bytes.")
            if profile.get('preserve_dependency_path'):
                notes+=(f"\nImportant: {profile['preserve_dependency_path']} is verified and preserved unchanged. Static code points to resource-key lookup, not proven duration fields. The replacement RIFF/fact/data metadata and header-cache slot are updated, but duration/next-track behaviour must be tested in PPSSPP.")
            if profile.get('patch_warning'):
                notes+=(f"\nValidation limitation: {profile['patch_warning']}")
            self.direct_notes_text.set(notes)
            self.radio_context_label.set(f"{profile['platform']} · {profile['asset_extension'].upper()} whole-station replacement · game-specific ISO target" + (" + header-cache update" if profile.get('header_companion_path') else ""))
            if hasattr(self,'audio_context_help'):
                help_text=f"{profile['title']} expects a finished whole-station {profile['asset_extension'].upper()} file at {profile['target_path']}. The generic Audio Preparation outputs are not a substitute for a fully compatible {profile['asset_extension'].upper()} stream; encode it with a target-specific tool before staging."
                if profile.get('header_companion_path'):
                    help_text+=f" Patch ISO also updates {profile['header_companion_path']} slot {profile['header_slot_index']} from the replacement's 0x800-byte prefix."
                if profile.get('preserve_dependency_path'):
                    help_text+=f" {profile['preserve_dependency_path']} is preserved unchanged for now because its record structure is not yet confirmed."
                self.audio_context_help.set(help_text)
            self.status.set(f"Selected {profile['title']} radio replacement mode.")

    def pick_direct_radio_asset(self):
        _game_id,profile=self._selected_game_profile()
        if profile['mode']!='direct':
            messagebox.showwarning('Radio target not verified',profile.get('research_note','This profile does not have a verified direct radio target.'))
            return
        ext=profile['asset_extension'].lower()
        if ext=='.vb': types=[('PS2 radio stream (*.VB)','*.VB'),('All files','*.*')]
        else: types=[('PSP radio stream (*.AT3)','*.AT3'),('All files','*.*')]
        selected=filedialog.askopenfilename(title=f"Select compatible {profile['asset_filename']} replacement",filetypes=types)
        if selected:
            self.direct_radio_source.set(selected)
            self.status.set(f"Selected {Path(selected).name} for {profile['title']}.")

    def prepare_direct_radio_file(self,game_id,profile):
        source_text=self.direct_radio_source.get().strip()
        if not source_text:
            self.pick_direct_radio_asset(); source_text=self.direct_radio_source.get().strip()
            if not source_text:return
        source=Path(source_text).expanduser().resolve()
        if not source.is_file() or source.stat().st_size<=0:
            raise ValueError('Choose a non-empty, existing compatible radio stream file.')
        if source.suffix.lower()!=profile['asset_extension']:
            raise ValueError(f"{profile['title']} requires a compatible {profile['asset_extension'].upper()} stream file. Selected: {source.name}")
        initial=Path(self.out_dir.get().strip()).expanduser() if self.out_dir.get().strip() else source.parent
        destination=filedialog.askdirectory(title='Choose staging root for the selected game radio file',initialdir=str(initial))
        if not destination:return
        root=Path(destination)
        preview=(f"Prepare the whole-station replacement for {profile['title']}?\n\n"
                 f"SOURCE: {source}\nTARGET: {root.joinpath(*profile['target_path'].split('/'))}\n"
                 f"DISC PATH: {profile['target_path']}\nSIZE: {source.stat().st_size:,} bytes\n\n"
                 "The asset is copied byte-for-byte; it is not converted. The manifest records its size and SHA-256. The ISO is not modified.")
        if not messagebox.askyesno('Prepare Radio File for ISO Patching',preview):return
        try:
            result=iso_patcher.prepare_game_radio_staging(source,root,game_id,overwrite=False,station_id=profile.get('station_id'))
        except FileExistsError as conflict:
            if not messagebox.askyesno('Staging files already exist',str(conflict)+'\n\nOverwrite those exact staging outputs? No backups will be created.'):
                return
            result=iso_patcher.prepare_game_radio_staging(source,root,game_id,overwrite=True,station_id=profile.get('station_id'))
        self.last_prepared_root=root; self.last_prepared_game_id=game_id; self.last_prepared_station_id=profile.get('station_id')
        self.status.set(f"Prepared {profile['asset_filename']} for {profile['title']}.")
        messagebox.showinfo('Radio file prepared',f"Game: {profile['title']}\nStation: {profile['station']}\nDisc path: {profile['target_path']}\nSize: {result['size_bytes']:,} bytes\nSHA-256: {result['sha256']}\nManifest: {result['manifest_path']}\n\nNext: click Patch ISO to create a separate patched image copy.")
    def _audio_ui(self):
        self._browse_row(self.audio_tab,'FFmpeg executable',self.ffmpeg,self.pick_ffmpeg)
        row=ttk.Frame(self.audio_tab); row.pack(fill='x',pady=8); ttk.Button(row,text='Add audio files…',command=self.add_audio).pack(side='left'); ttk.Button(row,text='Remove selected',command=self.remove_audio).pack(side='left',padx=6); ttk.Button(row,text='Clear queue',command=self.clear_audio).pack(side='left')
        ttk.Label(self.audio_tab,text='Target game / format').pack(anchor='w'); self.audio_format=tk.StringVar(value='GTA LCS PS2 VB'); ttk.Combobox(self.audio_tab,textvariable=self.audio_format,state='readonly',values=list(audio_profiles.PROFILES),width=36).pack(anchor='w',pady=4)
        stationrow=ttk.Frame(self.audio_tab); stationrow.pack(fill='x',pady=2)
        self.audio_ps2_station=tk.StringVar(value='')
        ttk.Label(stationrow,text='PS2 VB station name (optional):').pack(side='left')
        ttk.Entry(stationrow,textvariable=self.audio_ps2_station,width=19).pack(side='left',padx=6)
        ttk.Label(stationrow,text='CHAT (III), KCHAT / POLICE / VCPR (VC) use 16 kHz; otherwise 32 kHz.',style='Muted.TLabel').pack(side='left')
        self.audio_ps2_rate_override=tk.StringVar(value='Auto (station-based)')
        ttk.Label(stationrow,text='Rate:').pack(side='left',padx=(12,0))
        ttk.Combobox(stationrow,textvariable=self.audio_ps2_rate_override,state='readonly',values=['Auto (station-based)','16000 Hz','32000 Hz'],width=23).pack(side='left',padx=4)
        toolrow=ttk.Frame(self.audio_tab);toolrow.pack(fill='x',pady=2)
        self.audio_at3tool=tk.StringVar(value=audio_profiles.find_at3tool() or ''); ttk.Label(toolrow,text='PSP ATRAC3+ encoder (bundled)').pack(side='left');ttk.Entry(toolrow,textvariable=self.audio_at3tool).pack(side='left',fill='x',expand=True,padx=6)
        ttk.Button(toolrow,text='Browse…',command=lambda:self.audio_at3tool.set(filedialog.askopenfilename(title='Choose at3tool.exe',filetypes=[('Encoder','*.exe'),('All files','*.*')]))).pack(side='left')
        originalrow=ttk.Frame(self.audio_tab);originalrow.pack(fill='x',pady=2)
        self.audio_reference_at3=tk.StringVar();ttk.Label(originalrow,text='Original PSP AT3 reference').pack(side='left');ttk.Entry(originalrow,textvariable=self.audio_reference_at3).pack(side='left',fill='x',expand=True,padx=6)
        ttk.Button(originalrow,text='Browse…',command=lambda:self.audio_reference_at3.set(filedialog.askopenfilename(title='Choose original GTA PSP AT3',filetypes=[('ATRAC3plus','*.at3'),('All files','*.*')]))).pack(side='left')
        self.drop_label=ttk.Label(self.audio_tab,text='Drop audio files here to add them to the queue' + (' (drag-and-drop enabled)' if DND_AVAILABLE else ' (install tkinterdnd2 to enable drag-and-drop)'),anchor='center',padding=10,style='Muted.TLabel'); self.drop_label.pack(fill='x',pady=4)
        self.audio_tree=ttk.Treeview(self.audio_tab,columns=('file','size','state'),show='headings');
        for c,t,w in [('file','Source file',540),('size','Size',120),('state','Status',260)]: self.audio_tree.heading(c,text=t); self.audio_tree.column(c,width=w,anchor='w')
        self.audio_tree.pack(fill='both',expand=True,pady=8)
        if DND_AVAILABLE:
            for widget in (self.drop_label, self.audio_tree):
                widget.drop_target_register(DND_FILES); widget.dnd_bind('<<Drop>>', self.drop_audio)
        p=ttk.Frame(self.audio_tab); p.pack(fill='x'); self.audio_progress=ttk.Progressbar(p,mode='determinate',maximum=100); self.audio_progress.pack(side='left',fill='x',expand=True); self.audio_pct=tk.StringVar(value='0%'); ttk.Label(p,textvariable=self.audio_pct,width=7).pack(side='left',padx=8); ttk.Button(p,text='Convert queued files…',style='Accent.TButton',command=self.convert_audio).pack(side='left')
        self.audio_context_help=tk.StringVar(value='PS2 VB: 0x2000-byte stereo channel interleave, 32 kHz by default. Mono sources remain duplicated across L/R 8 KiB channel blocks; true stereo stays separate; GTA III CHAT / GTA VC KCHAT, POLICE, VCPR are 16 kHz. Enter the target station name to choose the correct rate. Raw VB contains no rate header, so gameplay verification is still necessary. PSP AT3: ATRAC3plus RIFF and source-duration metadata checked; test in PPSSPP. SA PS2 uses the Radio Installer.')
        ttk.Label(self.audio_tab,textvariable=self.audio_context_help,style='Muted.TLabel',wraplength=1000).pack(anchor='w',pady=6)
        self.audio_queue={}
    def open_tim2_converter(self):
        """Select the PNG-to-TIM2 workspace tab."""
        try:
            self.tabs.select(self.tim2_tab)
        except Exception:
            pass

    def _tim2_ui(self):
        self.tim2_input = tk.StringVar()
        self.tim2_output = tk.StringVar()
        self.tim2_template = tk.StringVar()
        self.tim2_use_template = tk.BooleanVar(value=False)
        self.tim2_profile = tk.StringVar(value='GTA LCS PS2')
        self.tim2_format = tk.StringVar(value='4-bit Indexed (16 colors)')
        self.tim2_alignment = tk.StringVar(value='16 bytes')
        self.tim2_resize = tk.StringVar(value='Keep source dimensions')
        self.tim2_dither = tk.BooleanVar(value=False)
        self.tim2_source_info = tk.StringVar(value='Choose or drop an image to inspect its dimensions and alpha channel.')
        self.tim2_template_info = tk.StringVar(value='Reference template is optional. Using one helps match an existing game texture.')
        self.tim2_result_info = tk.StringVar(value='LCS/VCS PS2 and PSP sample-based presets. Choose an original TM2 template for matching asset metadata and GS layout.')

        intro = ttk.Label(self.tim2_tab, text='Convert PNG into Sony TIM2 (.TM2) for GTA LCS/VCS PS2 or PSP. Match the original .TM2 as a template where possible; GTA SA PS2 uses native RenderWare TXD editing instead.', style='Muted.TLabel', wraplength=1030)
        intro.pack(anchor='w', pady=(0, 8))

        files = ttk.LabelFrame(self.tim2_tab, text='Image and output', padding=8)
        files.pack(fill='x', pady=(0, 8))
        row = ttk.Frame(files); row.pack(fill='x', pady=3)
        ttk.Label(row, text='PNG / image', width=17).pack(side='left')
        self.tim2_input_entry = ttk.Entry(row, textvariable=self.tim2_input)
        self.tim2_input_entry.pack(side='left', fill='x', expand=True, padx=6)
        ttk.Button(row, text='Browse…', command=self._browse_tim2_input).pack(side='left')
        if DND_AVAILABLE:
            try:
                self.tim2_input_entry.drop_target_register(DND_FILES)
                self.tim2_input_entry.dnd_bind('<<Drop>>', self._drop_tim2_image)
            except Exception:
                pass
        ttk.Label(files, textvariable=self.tim2_source_info, style='Muted.TLabel', wraplength=1020).pack(anchor='w', padx=(118, 0), pady=(0, 4))

        row = ttk.Frame(files); row.pack(fill='x', pady=3)
        ttk.Label(row, text='Output .TM2', width=17).pack(side='left')
        ttk.Entry(row, textvariable=self.tim2_output).pack(side='left', fill='x', expand=True, padx=6)
        ttk.Button(row, text='Save as…', command=self._browse_tim2_output).pack(side='left')

        row = ttk.Frame(files); row.pack(fill='x', pady=(7, 2))
        ttk.Label(row, text='Reference .TM2', width=17).pack(side='left')
        ttk.Entry(row, textvariable=self.tim2_template).pack(side='left', fill='x', expand=True, padx=6)
        ttk.Button(row, text='Choose template…', command=self._browse_tim2_template).pack(side='left')
        ttk.Checkbutton(files, text='Match reference texture dimensions, pixel format, CLUT type, alignment and GS register fields', variable=self.tim2_use_template).pack(anchor='w', padx=(118, 0), pady=(2, 2))
        ttk.Label(files, textvariable=self.tim2_template_info, style='Muted.TLabel', wraplength=1020).pack(anchor='w', padx=(118, 0))

        opts = ttk.LabelFrame(self.tim2_tab, text='Conversion profile', padding=8)
        opts.pack(fill='x', pady=(0, 8))
        row = ttk.Frame(opts); row.pack(fill='x', pady=3)
        ttk.Label(row, text='Target profile', width=17).pack(side='left')
        self.tim2_profile_combo = ttk.Combobox(row, textvariable=self.tim2_profile, state='readonly', width=39,
            values=['GTA LCS PS2', 'GTA VCS PS2', 'GTA LCS PSP', 'GTA VCS PSP', 'Custom settings'])
        self.tim2_profile_combo.pack(side='left', padx=6)
        self.tim2_profile_combo.bind('<<ComboboxSelected>>', self._tim2_profile_changed)
        ttk.Label(row, text='Sample-derived layout: PAL4 LCS PSP; PAL8 VCS PS2. A template is recommended.', style='Muted.TLabel').pack(side='left', padx=8)

        row = ttk.Frame(opts); row.pack(fill='x', pady=3)
        ttk.Label(row, text='Image format', width=17).pack(side='left')
        ttk.Combobox(row, textvariable=self.tim2_format, state='readonly', width=30,
            values=list(tim2_converter.FORMAT_LABELS.keys())).pack(side='left', padx=6)
        ttk.Label(row, text='Picture-block alignment').pack(side='left', padx=(14, 6))
        ttk.Combobox(row, textvariable=self.tim2_alignment, state='readonly', width=20,
            values=['16 bytes', '128 bytes']).pack(side='left')

        row = ttk.Frame(opts); row.pack(fill='x', pady=3)
        ttk.Label(row, text='Dimensions', width=17).pack(side='left')
        ttk.Combobox(row, textvariable=self.tim2_resize, state='readonly', width=30,
            values=['Keep source dimensions', 'Resize up to powers of 2', 'Resize down to powers of 2']).pack(side='left', padx=6)
        ttk.Checkbutton(row, text='Dither indexed colors (4/8-bit)', variable=self.tim2_dither).pack(side='left', padx=(14, 0))

        actions = ttk.Frame(self.tim2_tab); actions.pack(fill='x', pady=(1, 8))
        ttk.Button(actions, text='Convert to TIM2', style='Accent.TButton', command=self._convert_tim2).pack(side='left')
        ttk.Button(actions, text='Verify output TIM2…', command=self._verify_tim2_output).pack(side='left', padx=7)
        ttk.Label(self.tim2_tab, textvariable=self.tim2_result_info, style='Muted.TLabel', wraplength=1030).pack(anchor='w', pady=(0, 6))
        note = ('Compatibility note: a valid TIM2 container is not automatically a drop-in replacement for every GTA asset. '
                'This encoder writes row-major pixel payloads and does not infer or reproduce per-asset GS swizzle/tile layouts; '
                'template mode preserves declared GS register fields but does not decode or rebuild such pixel layouts. '
                'Dimensions, pixel format, CLUT encoding, alpha, and the target game importer can matter. '
                'Multi-picture and mipmapped templates are refused rather than silently discarding unknown data. '
                'Use a target-game texture importer and verify the result in the matching game/emulator before replacing an asset.')
        ttk.Label(self.tim2_tab, text=note, style='Muted.TLabel', wraplength=1030).pack(anchor='w')

    def _tim2_profile_changed(self, _event=None):
        profile = self.tim2_profile.get()
        if profile in ('GTA LCS PS2', 'GTA VCS PS2', 'GTA LCS PSP', 'GTA VCS PSP'):
            self.tim2_format.set('8-bit Indexed (256 colors)' if profile == 'GTA VCS PS2' else '4-bit Indexed (16 colors)')
            self.tim2_alignment.set('16 bytes')
            self.tim2_resize.set('Keep source dimensions')
            self.tim2_dither.set(False)
            self.tim2_result_info.set(profile + ': target sample-based default. VCS PS2 radio_4.tm2 is 64×64 PAL8/256 colors; LCS PSP kjah.tm2 is 64×64 PAL4/16 colors. Other asset layouts may differ—use its original TM2 as template.')
        elif profile == 'Generic PS2 TIM2':
            self.tim2_format.set('32-bit RGBA8888')
            self.tim2_alignment.set('128 bytes')
            self.tim2_resize.set('Keep source dimensions')
            self.tim2_dither.set(False)
            self.tim2_result_info.set('Generic PS2 preset selected: 32-bit RGBA8888, 128-byte picture-block alignment, no mipmaps and no pre-swizzle.')
        else:
            self.tim2_result_info.set('Custom settings selected. Choose an image format and alignment suited to the target game or reference texture.')

    def _browse_tim2_input(self):
        path = filedialog.askopenfilename(title='Choose source PNG/image', filetypes=[('Images', '*.png *.jpg *.jpeg *.bmp *.tga *.tif *.tiff *.webp *.gif *.ico'), ('All files', '*.*')])
        if not path:
            return
        self.tim2_input.set(path)
        if not self.tim2_output.get().strip():
            self.tim2_output.set(str(Path(path).with_suffix('.tm2')))
        else:
            # Keep an existing user-selected output if it does not point to the previous input's default.
            old_out = Path(self.tim2_output.get()) if self.tim2_output.get().strip() else None
            if old_out and old_out.suffix.lower() != '.tm2':
                self.tim2_output.set(str(Path(path).with_suffix('.tm2')))
        self._refresh_tim2_source_info()

    def _drop_tim2_image(self, event):
        try:
            paths = self.tk.splitlist(event.data)
            if paths:
                path = Path(paths[0])
                if path.suffix.lower() not in ('.png', '.jpg', '.jpeg', '.bmp', '.tga', '.tif', '.tiff', '.webp', '.gif', '.ico', '.ppm', '.pgm', '.pbm'):
                    messagebox.showwarning('TIM2 converter', 'Drop an image file such as a PNG.', parent=self)
                    return
                self.tim2_input.set(str(path))
                if not self.tim2_output.get().strip():
                    self.tim2_output.set(str(path.with_suffix('.tm2')))
                self._refresh_tim2_source_info()
        except Exception as exc:
            messagebox.showerror('TIM2 converter', f'Could not read the dropped image path:\n{exc}', parent=self)

    def _refresh_tim2_source_info(self):
        source = self.tim2_input.get().strip()
        if not source:
            self.tim2_source_info.set('Choose or drop an image to inspect its dimensions and alpha channel.')
            return
        try:
            from PIL import Image as PILImage
            with PILImage.open(source) as img:
                w, h = img.size
                has_alpha = ('A' in img.getbands()) or ('transparency' in img.info)
                self.tim2_source_info.set(f'Source: {w} × {h} px · mode {img.mode} · alpha/transparency: {"present" if has_alpha else "not detected"} · {Path(source).name}')
            if not self.tim2_output.get().strip():
                self.tim2_output.set(str(Path(source).with_suffix('.tm2')))
        except Exception as exc:
            self.tim2_source_info.set(f'Image inspection failed: {exc}')

    def _browse_tim2_template(self):
        path = filedialog.askopenfilename(title='Choose original TIM2 texture template', filetypes=[('TIM2 textures', '*.tm2 *.tim2'), ('All files', '*.*')])
        if not path:
            return
        self.tim2_template.set(path)
        self.tim2_use_template.set(True)
        try:
            info = tim2_converter.inspect_tim2(path)
            self.tim2_template_info.set(f'Template: {info["width"]} × {info["height"]} px · {info["image_format"]} · {info["clut_colors"]} CLUT colors · {info["mipmap_count"]} mip level(s) · {info["file_size"]:,} bytes')
        except Exception as exc:
            self.tim2_template_info.set(f'Template inspection failed: {exc}')

    def _browse_tim2_output(self):
        source = self.tim2_input.get().strip()
        initial = str(Path(source).with_suffix('.tm2')) if source else 'texture.tm2'
        path = filedialog.asksaveasfilename(title='Save TIM2 texture as', initialfile=Path(initial).name, initialdir=str(Path(initial).parent) if Path(initial).parent.exists() else None, defaultextension='.tm2', filetypes=[('TIM2 textures', '*.tm2'), ('All files', '*.*')])
        if path:
            self.tim2_output.set(path)

    def _convert_tim2(self):
        source = self.tim2_input.get().strip()
        output = self.tim2_output.get().strip()
        template = self.tim2_template.get().strip()
        use_template = self.tim2_use_template.get()
        if not source:
            messagebox.showinfo('TIM2 Converter', 'Choose a PNG or supported image first.', parent=self); return
        if not output:
            output = str(Path(source).with_suffix('.tm2'))
            self.tim2_output.set(output)
        if use_template and not template:
            messagebox.showinfo('TIM2 Converter', 'The template option is enabled but no reference .TM2 file is selected.', parent=self); return
        if Path(source).resolve() == Path(output).resolve():
            messagebox.showerror('TIM2 Converter', 'The output path must not overwrite the source image.', parent=self); return
        if Path(output).exists():
            if not messagebox.askyesno('Overwrite existing TIM2?', f'This file already exists:\n{output}\n\nOverwrite it?', parent=self):
                return
        try:
            fmt = tim2_converter.FORMAT_LABELS[self.tim2_format.get()]
            alignment = int(self.tim2_alignment.get().split()[0])
            result = tim2_converter.convert_png_to_tim2(
                source, output,
                image_format=fmt,
                alignment=alignment,
                resize_mode=self.tim2_resize.get(),
                dither=self.tim2_dither.get(),
                template_path=(template if use_template else None),
            )
            checked = tim2_converter.verify_tim2(output)
            template_text = f'\nReference matched: {Path(template).name}' if result['used_template'] else ''
            dim_text = ''
            if result['input_dimensions'] != (result['width'], result['height']):
                dim_text = f'\nDimensions: {result["input_dimensions"][0]} × {result["input_dimensions"][1]} → {result["width"]} × {result["height"]}'
            message = (f'Converted and structurally verified:\n{result["output"]}\n\n'
                       f'TIM2: {result["width"]} × {result["height"]} · {result["format"]}\n'
                       f'Picture alignment: {result["alignment"]} bytes\n'
                       f'Image data: {result["image_size"]:,} bytes · CLUT: {result["clut_size"]:,} bytes\n'
                       f'File size: {result["file_size"]:,} bytes{dim_text}{template_text}\n\n'
                       'The output passed structural checks. Test it in the matching game/importer before replacing an in-game asset.')
            self.tim2_result_info.set(f'Created and verified {Path(output).name} · {checked["width"]} × {checked["height"]} · {checked["image_format"]} · {checked["file_size"]:,} bytes.')
            self.status.set(f'Created TIM2 texture: {Path(output).name}.')
            messagebox.showinfo('TIM2 conversion complete', message, parent=self)
        except Exception as exc:
            messagebox.showerror('TIM2 conversion failed', str(exc), parent=self)

    def _verify_tim2_output(self):
        path = self.tim2_output.get().strip()
        if not path or not Path(path).is_file():
            path = filedialog.askopenfilename(title='Choose TIM2 file to verify', filetypes=[('TIM2 textures', '*.tm2 *.tim2'), ('All files', '*.*')])
            if not path:
                return
        try:
            info = tim2_converter.verify_tim2(path)
            message = (f'Valid one-picture TIM2 structure\n\nFile: {Path(path).name}\n'
                       f'Dimensions: {info["width"]} × {info["height"]}\nFormat: {info["image_format"]}\n'
                       f'Image data: {info["image_size"]:,} bytes\nCLUT data: {info["clut_size"]:,} bytes\n'
                       f'Picture block: {info["total_size"]:,} bytes\nFile size: {info["file_size"]:,} bytes')
            messagebox.showinfo('TIM2 verification', message, parent=self)
        except Exception as exc:
            messagebox.showerror('TIM2 verification failed', str(exc), parent=self)

    def _legacy__txd_ui(self):
        # Keep a direct path entry independent from the general open menu.
        self.txd_path_var=tk.StringVar(); self._browse_row(self.txd_tab,'TXD file',self.txd_path_var,self.open_txd)
        bar=ttk.Frame(self.txd_tab); bar.pack(fill='x',pady=5); ttk.Button(bar,text='Inspect dictionary',command=self.inspect_txd).pack(side='left'); ttk.Button(bar,text='Export selected chunk…',command=self.export_txd_chunk).pack(side='left',padx=6)
        self.txd_tree=ttk.Treeview(self.txd_tab,columns=('offset','type','size','detail'),show='headings')
        for c,t,w in [('offset','Offset',110),('type','RenderWare chunk',220),('size','Payload size',130),('detail','Header / validation',560)]: self.txd_tree.heading(c,text=t); self.txd_tree.column(c,width=w,anchor='w')
        self.txd_tree.pack(fill='both',expand=True,pady=5); ttk.Label(self.txd_tab,text='Native RenderWare structure viewer. Texture pixel decode/import and dictionary rebuild are not yet enabled; exporting a chunk preserves its original bytes.',style='Muted.TLabel').pack(anchor='w')
    def _gxt_ui(self):
        self.gxt_path_var=tk.StringVar(); self._browse_row(self.gxt_tab,'GXT file',self.gxt_path_var,self.open_gxt)
        bar=ttk.Frame(self.gxt_tab); bar.pack(fill='x',pady=5); ttk.Label(bar,text='Filter').pack(side='left'); self.gxt_filter=tk.StringVar(); e=ttk.Entry(bar,textvariable=self.gxt_filter,width=36); e.pack(side='left',padx=6); e.bind('<KeyRelease>',lambda _e:self.refresh_gxt()); ttk.Button(bar,text='Edit selected…',command=self.edit_gxt).pack(side='left',padx=6); ttk.Button(bar,text='Save edits as…',style='Accent.TButton',command=self.save_gxt).pack(side='left')
        self.gxt_tree=ttk.Treeview(self.gxt_tab,columns=('table','key','text','slot'),show='headings')
        for c,t,w in [('table','Table',130),('key','Key / CRC32',120),('text','String text',650),('slot','Slot bytes',90)]: self.gxt_tree.heading(c,text=t); self.gxt_tree.column(c,width=w,anchor='w')
        self.gxt_tree.pack(fill='both',expand=True,pady=5); self.gxt_info=tk.StringVar(value='Open a GXT file to parse its table directory and strings.'); ttk.Label(self.gxt_tab,textvariable=self.gxt_info,style='Muted.TLabel').pack(anchor='w')
        ttk.Label(self.gxt_tab,text='This editor uses offset-preserving replacement: new text must fit the existing string slot. Longer strings are refused rather than corrupting platform-specific offsets.',style='Muted.TLabel',wraplength=1000).pack(anchor='w',pady=3)
    def _legacy__dtz_ui(self):
        self.dtz_path_var=tk.StringVar(); self._browse_row(self.dtz_tab,'GAME.DTZ',self.dtz_path_var,self.open_dtz)
        bar=ttk.Frame(self.dtz_tab); bar.pack(fill='x',pady=5); ttk.Button(bar,text='Inspect / decompress',command=self.inspect_dtz).pack(side='left'); ttk.Button(bar,text='Save decompressed data…',command=self.save_dtz_raw).pack(side='left',padx=6); ttk.Button(bar,text='Rebuild compressed DTZ…',style='Accent.TButton',command=self.rebuild_dtz).pack(side='left')
        self.dtz_info=tk.Text(self.dtz_tab,height=18,bg='#0b1220',fg=FG,insertbackground='white',wrap='word'); self.dtz_info.pack(fill='both',expand=True,pady=5); ttk.Label(self.dtz_tab,text='This handles the zlib-wrapped GAME.DTZ samples present in the reference bundle. It recompresses the complete decompressed stream; it does not yet edit individual texture/config resources inside GAME.DAT.',style='Muted.TLabel',wraplength=1000).pack(anchor='w')
    def _poll(self):
        try:
            while True:
                kind,payload=self.q.get_nowait()
                if kind=='status': self.status.set(payload)
                elif kind=='audio_row':
                    iid=payload['iid']; self.audio_tree.set(iid,'state',payload['state'])
                elif kind=='audio_progress': self.audio_progress['value']=max(0,min(100,float(payload))); self.audio_pct.set(f'{float(payload):.0f}%')
                elif kind=='audio_done':
                    self.busy=False; self.status.set(payload); messagebox.showinfo('Audio conversion',payload)
                elif kind=='replace_done':
                    self.busy=False; self.status.set(payload); messagebox.showinfo('Track replaced',payload)
                elif kind=='replace_error':
                    self.busy=False; self.status.set('Track replacement failed.'); messagebox.showerror('Track replacement failed',payload)
                elif kind=='iso_patch_progress':
                    self.status.set(str(payload))
                elif kind=='iso_patch_done':
                    self.busy=False
                    self.patch_iso_button.configure(state='normal')
                    result=payload
                    try:
                        self.game_combo.configure(state='readonly')
                        self._refresh_station_options()
                    except Exception:
                        pass
                    game_title=result.get('game_title','Grand Theft Auto: San Andreas (PS2)')
                    station=result.get('station') or result.get('archive_name','radio asset')
                    output_image=result.get('output_image') or result.get('output_iso')
                    lines=[
                        'Patched image created and structurally verified.',
                        f'Game: {game_title} ({result["serial"]})',
                        f'Station / asset: {station}',
                        f'Output: {output_image}',
                        f'Output size: {result["output_size_bytes"]:,} bytes',
                        f'Report: {result["report_path"] or "not written"}', '',
                        'The source image was not modified.',
                        'Open the patched image in the appropriate emulator and verify the station audio in-game.'
                    ]
                    for item in result['files']:
                        suffix = '; relocated to new ISO sectors' if item['relocated'] else ''
                        lines.append(f"{item['game_disc_path']}: {item['old_size_bytes']:,} → {item['new_size_bytes']:,} bytes; SHA-256 verified{suffix}")
                    cache_update=result.get('header_cache_update')
                    if cache_update:
                        lines.append(f"Header cache updated: {cache_update['game_disc_path']} slot {cache_update['slot_index']} at 0x{cache_update['slot_offset_bytes']:X} ({cache_update['slot_size_bytes']:,} bytes).")
                        if 'original_declared_duration_seconds' in cache_update:
                            lines.append(f"Declared duration: {cache_update['original_declared_duration_seconds']:.3f}s → {cache_update['replacement_declared_duration_seconds']:.3f}s ({cache_update['duration_delta_seconds']:+.3f}s).")
                    for dependency in result.get('preserved_dependencies',[]):
                        lines.append(f"Dependency preserved unchanged: {dependency['game_disc_path']} (SHA-256 verified).")
                    if result.get('warning'):
                        lines.extend(['', f"Important validation note: {result['warning']}"])
                    if result.get('report_warning'):
                        lines.extend(['', result['report_warning']])
                    self.status.set(f'Patch ISO complete: {Path(output_image).name}; file hashes verified.')
                    messagebox.showinfo('ISO patch complete','\n'.join(lines))
                elif kind=='iso_patch_error':
                    self.busy=False
                    try:
                        self.game_combo.configure(state='readonly')
                        self._refresh_station_options()
                    except Exception: pass
                    self.patch_iso_button.configure(state='normal')
                    self.status.set('Patch ISO failed; source image is unchanged.')
                    messagebox.showerror('Patch ISO failed',str(payload))
                elif kind=='batch_replace_progress':
                    value=max(0,min(100,float(payload.get('value',0))))
                    self.radio_progress['value']=value; self.radio_progress_pct.set(f'{value:.0f}%')
                    self.radio_progress_text.set(payload.get('text','Batch replacement running…'))
                elif kind=='batch_replace_done':
                    self.busy=False
                    successes=payload.get('successes',[]); failures=payload.get('failures',[]); total=payload.get('total',len(successes)+len(failures))
                    final_percent = 100.0
                    self.radio_progress['value']=final_percent
                    self.radio_progress_pct.set(f'{final_percent:.0f}%')
                    report_path=payload.get('report_path','')
                    if failures:
                        self.status.set(f'Batch replacement finished: {len(successes)} converted, {len(failures)} failed.')
                        self.radio_progress_text.set(f'Finished batch: {len(successes)} converted, {len(failures)} failed. Review the report before rebuilding.')
                        lines=[f'Batch replacement finished: {len(successes)} succeeded, {len(failures)} failed, of {total}.',
                               'Successful entries were converted and placed in the extracted tracks folder.',
                               'Failed entries were left unchanged. Review failures before rebuilding.',
                               f'Report: {report_path}', '', 'Failures:']
                        lines.extend(f'Entry {x["record"]:04d} / {x["source"]}: {x["error"][:260]}' for x in failures[:12])
                        if len(failures)>12: lines.append(f'… plus {len(failures)-12} more failure(s).')
                        messagebox.showwarning('Batch replacement completed with errors','\n'.join(lines))
                    else:
                        self.status.set(f'Batch replacement complete: {len(successes)} tracks converted.')
                        self.radio_progress_text.set(f'Converted {len(successes)} track(s). Click Rebuild Archive, then Prepare Files for ISO Patching.')
                        messagebox.showinfo('Batch replacement complete',f'Converted {len(successes)} replacement song(s) into GTA SA PS2-compatible radio data.\n\nReport: {report_path}\n\nNext: click Rebuild Archive, then Prepare Files for ISO Patching. Use your existing ISO patcher to patch a test ISO and run that ISO in PCSX2.')
                elif kind=='audio_error': self.busy=False; messagebox.showerror('Audio conversion failed',payload); self.status.set('Audio conversion failed.')
        except queue.Empty: pass
        self.after(100,self._poll)
    def pick_pak(self):
        p=filedialog.askopenfilename(title='Open GTA SA radio PAK',filetypes=[('PAK archive','*.PAK'),('All files','*.*')]);
        if p: self.pak.set(p); self.status.set(f'PAK selected: {Path(p).name}')
    def pick_lookup(self):
        p=filedialog.askopenfilename(title='Open TRAKLKUP.DAT',filetypes=[('Lookup table','*.DAT'),('All files','*.*')]);
        if p: self.lookup.set(p)
    def choose_tracks(self):
        p=filedialog.askdirectory(title='Select extracted track folder');
        if p: self.extract_dir.set(p)
    def choose_output(self):
        p=filedialog.askdirectory(title='Select output folder');
        if p: self.out_dir.set(p)
    def pick_ffmpeg(self):
        p=filedialog.askopenfilename(title='Select ffmpeg executable',filetypes=[('FFmpeg','ffmpeg.exe'),('Executables','*.exe'),('All files','*.*')]);
        if p: self.ffmpeg.set(p)
    def _show_radio_context_menu(self, event):
        # Right-clicking an unselected row makes that row the current selection;
        # right-clicking within a multi-selection preserves the whole selection.
        row = self.radio_tree.identify_row(event.y)
        if row and row not in self.radio_tree.selection():
            self.radio_tree.selection_set(row)
            self.radio_tree.focus(row)
        try:
            self.radio_context_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.radio_context_menu.grab_release()
        return 'break'

    def select_all_radio_rows(self):
        rows = self.radio_tree.get_children('')
        if rows:
            self.radio_tree.selection_set(rows)
            self.radio_tree.focus(rows[0])
            self.status.set(f'Selected all {len(rows)} archive rows. Batch Replace Songs needs one source file per selected supported entry.')
        else:
            self.status.set('No archive rows to select. Inspect a radio archive first.')

    def select_supported_radio_rows(self):
        rows = []
        for iid in self.radio_tree.get_children(''):
            kind = str(self.radio_tree.set(iid, 'kind')).strip().casefold()
            if kind.startswith('gta sa type-4 stream') or kind == 'vagp':
                rows.append(iid)
        self.radio_tree.selection_set(rows)
        if rows:
            self.radio_tree.focus(rows[0])
        self.status.set(f'Selected {len(rows)} supported audio entries; raw/unknown records were excluded.')

    def clear_radio_selection(self):
        self.radio_tree.selection_remove(self.radio_tree.selection())
        self.status.set('Radio archive row selection cleared. Batch replacement will map files from the first supported entry in archive order.')

    def invert_radio_selection(self):
        selected = set(self.radio_tree.selection())
        rows = self.radio_tree.get_children('')
        new_selection = [iid for iid in rows if iid not in selected]
        self.radio_tree.selection_set(new_selection)
        if new_selection:
            self.radio_tree.focus(new_selection[0])
        self.status.set(f'Selected {len(new_selection)} of {len(rows)} archive rows.')

    def _radio_select_all_shortcut(self, _event=None):
        self.select_all_radio_rows()
        return 'break'

    def _radio_args(self):
        if not self.pak.get().strip() or not self.lookup.get().strip(): raise ValueError('Choose both a radio PAK and its matching TRAKLKUP.DAT.')
        return Path(self.pak.get()),Path(self.lookup.get()),self.group.get(),self.offset_unit.get(),self.size_unit.get()
    def inspect_radio(self):
        if self._selected_game_profile()[1]['mode']!='sa_pak':
            messagebox.showinfo('Game-specific workflow','This game uses a direct whole-station .VB/.AT3 file, not a San Andreas PAK + lookup table.')
            return
        try:
            args=self._radio_args(); res=core.inspect_archive(*args); self.radio_tree.delete(*self.radio_tree.get_children())
            pak,lk,allr,gid,off,size,tracks,stats=res
            for tr in tracks:
                kind='GTA SA type-4 stream' if tr.has_stream_header else ('VAGp' if tr.is_vag else 'Unknown/raw')
                self.radio_tree.insert('','end',iid=str(tr.record.index),values=(f'{tr.record.index:04d} / {tr.record.archive_id:02X}',f'0x{tr.offset_bytes:X} ({human(tr.offset_bytes)})',human(tr.stored_size_bytes or 0),kind,f'{tr.sample_rate or "—"} Hz'))
            self.group.set(f'0x{gid:02X}'); self.status.set(f'Inspected {len(tracks)} tracks · archive ID 0x{gid:02X} · offsets {off} · sizes {size}.')
            messagebox.showinfo('Archive inspection',f'Found {len(tracks)} records for archive ID 0x{gid:02X}.\nOffset units: {off}\nSize units: {size}\n\nUse Extract Tracks before replacing audio.')
        except Exception as e: messagebox.showerror('Inspection failed',str(e))
    def extract_radio(self):
        try:
            pak,lookup,g,o,s=self._radio_args(); out=self.extract_dir.get().strip()
            if not out: out=filedialog.askdirectory(title='Choose folder for extracted tracks')
            if not out:return
            ok,skipped,ou,su=core.extract_tracks(pak,lookup,Path(out),g,o,s); self.extract_dir.set(out); self.status.set(f'Extracted {ok} entries.'); messagebox.showinfo('Extraction complete',f'Extracted {ok} entries. Skipped: {skipped}.\nOffset units: {ou}\nSize units: {su}')
            self.inspect_radio()
        except Exception as e: messagebox.showerror('Extraction failed',str(e))
    def auto_rename_tracks(self):
        try:
            pak,lookup,g,o,s=self._radio_args(); folder=self.extract_dir.get().strip()
            if not folder: raise ValueError('Choose the folder containing converted track files first.')
            res=core.inspect_archive(pak,lookup,g,o,s); tracks=res[6]
            plan=core.build_auto_rename_plan(Path(folder),tracks)
            preview='\n'.join(f'{x["source"].name}  →  {x["target"].name}  [{x["method"]}]' for x in plan[:18])
            if len(plan)>18: preview+=f'\n… and {len(plan)-18} more file(s)'
            if not messagebox.askyesno('Confirm filename mapping',f'Prepared {len(plan)} rename(s) using the archive lookup records.\n\n{preview}\n\nApply this mapping? No files will be overwritten.'):
                return
            count=core.apply_auto_rename_plan(plan); self.status.set(f'Auto-renamed {count} track file(s).'); messagebox.showinfo('Auto-rename complete',f'Renamed {count} file(s).')
        except Exception as e: messagebox.showerror('Auto-rename failed',str(e))
    def replace_selected_track(self):
        sel=self.radio_tree.selection()
        if not sel:
            messagebox.showinfo('Replace track','Inspect the PAK and select a track row first.'); return
        try:
            pak,lookup,g,o,s=self._radio_args()
            res=core.inspect_archive(pak,lookup,g,o,s)
            tr=next((x for x in res[6] if x.record.index==int(sel[0])),None)
            if tr is None: raise ValueError('The selected lookup record was not found in the inspected archive.')
            folder_text=self.extract_dir.get().strip()
            if not folder_text: raise ValueError('Extract the archive first, then select the extracted tracks folder.')
            folder=Path(folder_text); idx=tr.record.index
            target=(folder/f'entry_{idx:04d}.bin') if tr.has_stream_header or not tr.is_vag else (folder/f'entry_{idx:04d}.vag')
            if not target.exists(): raise ValueError(f'Missing {target.name}. Extract this archive to the selected tracks folder first.')
            if not tr.has_stream_header and not tr.is_vag:
                raise ValueError('This entry is not recognized as VAGp or a GTA SA type-4 stream. Replacement is blocked to avoid writing an incompatible audio layout.')
            source=filedialog.askopenfilename(title=f'Replace record {idx:04d}',filetypes=[('Audio files','*.wav *.mp3 *.ogg *.flac *.m4a *.aac *.wma *.aiff'),('All files','*.*')])
            if not source:return
            ff=self.ffmpeg.get().strip() or shutil.which('ffmpeg')
            if not ff: raise ValueError('FFmpeg is required. Install it or choose ffmpeg.exe in Audio Preparation.')
            self.busy=True; self.status.set(f'Preparing replacement for record {idx:04d}…')
            def worker():
                try:
                    with tempfile.TemporaryDirectory(prefix='gta_studio_replace_') as td:
                        stage=Path(td); rate=tr.sample_rate or 24000; raw=stage/'full.adpcm'
                        audio.convert_audio_to_ps2_payload(Path(source),raw,ff,rate)
                        if tr.has_stream_header:
                            hdr=folder/f'entry_{idx:04d}.hdr'
                            if not hdr.is_file(): raise ValueError(f'Missing {hdr.name}; re-extract so the original stream header is preserved.')
                            mini=stage/'mini.adpcm'; audio.convert_audio_to_ps2_payload(Path(source),mini,ff,audio.GTASA_MINI_RATE)
                            packed=stage/'packed.bin'; meta=audio.pack_gtasa_ps2_stream_payload(raw,mini,packed)
                            new_header=audio.update_gtasa_type4_stream_header(hdr.read_bytes(),meta,rate)
                            stream_check=audio.validate_gtasa_type4_stream(new_header,packed,expected_full_rate=rate)
                            # Commit only after chunk, sample-rate, frame, duration, start, and end-marker checks pass.
                            target.write_bytes(packed.read_bytes()); hdr.write_bytes(new_header)
                        else:
                            vag=stage/'replacement.vag'; audio.wrap_vagp(raw,vag,rate); core.read_vag_size(vag)
                            target.write_bytes(vag.read_bytes())
                    self.q.put(('replace_done',f'Replaced record {idx:04d} with {Path(source).name}. Rebuild the archive to write the new PAK.'))
                except Exception as exc:self.q.put(('replace_error',str(exc)))
            threading.Thread(target=worker,daemon=True).start()
        except Exception as e: messagebox.showerror('Replacement failed',str(e))
    def drop_radio_audio(self, event):
        """Accept dropped audio files or a folder directly in the radio browser."""
        try:
            self.batch_replace_tracks(self.tk.splitlist(event.data))
        except Exception as exc:
            messagebox.showerror('Radio audio drop failed', str(exc))

    def batch_replace_tracks(self, source_paths=None):
        """Batch-transcode common audio formats into selected GTA SA PS2 PAK entries."""
        if self.busy:
            messagebox.showinfo('Batch replacement', 'A conversion is already running.')
            return
        try:
            pak, lookup, g, o, s = self._radio_args()
            folder_text = self.extract_dir.get().strip()
            if not folder_text:
                raise ValueError('Extract the radio archive first, then select the extracted tracks folder.')
            folder = Path(folder_text)
            if not folder.is_dir():
                raise ValueError(f'The extracted tracks folder does not exist: {folder}')
            if source_paths is None:
                selected_paths = filedialog.askopenfilenames(
                    title='Choose replacement songs (files are naturally sorted by filename)',
                    filetypes=[('Audio files', '*.wav *.wave *.mp3 *.flac *.ogg *.oga *.opus *.m4a *.aac *.wma *.aiff *.aif *.ape *.mp2 *.ac3 *.at3 *.at3p *.caf *.mp4 *.webm *.mkv *.mov'), ('All files', '*.*')])
                if not selected_paths:
                    return
            else:
                selected_paths = source_paths
            expanded = []
            for value in selected_paths:
                candidate = Path(value)
                if candidate.is_dir():
                    expanded.extend(child for child in candidate.iterdir()
                                    if child.is_file() and child.suffix.lower() in audio.SUPPORTED_AUDIO_SUFFIXES)
                elif candidate.is_file():
                    expanded.append(candidate)
            # Ignore exact duplicate paths but keep different files with identical stems.
            unique = []
            seen_paths = set()
            for candidate in expanded:
                identity = str(candidate.resolve()).casefold()
                if identity not in seen_paths:
                    unique.append(candidate); seen_paths.add(identity)
            sources = sorted(unique, key=lambda p: self._natural_audio_key(p.name))
            if not sources:
                raise ValueError('No supported audio files were found in the selection/drop. Choose audio files or drop a folder containing them.')
            unsupported = [p for p in sources if p.suffix.lower() not in audio.SUPPORTED_AUDIO_SUFFIXES]
            if unsupported:
                raise ValueError('Unsupported audio type: ' + ', '.join(p.name for p in unsupported[:5]))
            ff = self.ffmpeg.get().strip() or shutil.which('ffmpeg')
            if not ff:
                raise ValueError('FFmpeg is required. Install it or select ffmpeg.exe in Audio Preparation.')
            _, _, _, gid, off_unit, size_unit, tracks, _stats = core.inspect_archive(pak, lookup, g, o, s)
            replaceable = [tr for tr in tracks if tr.has_stream_header or tr.is_vag]
            by_index = {tr.record.index: tr for tr in replaceable}
            selection = list(self.radio_tree.selection())
            if selection:
                selected_indexes = []
                for iid in selection:
                    try: selected_indexes.append(int(iid))
                    except ValueError: continue
                missing = [i for i in selected_indexes if i not in by_index]
                if missing:
                    raise ValueError('Your selection includes raw/unknown records that cannot safely be replaced as radio audio. Select only rows identified as GTA SA type-4 stream or VAGp.')
                target_tracks = sorted((by_index[i] for i in set(selected_indexes)), key=lambda tr: tr.offset_bytes)
                if len(target_tracks) != len(sources):
                    raise ValueError(f'You selected {len(target_tracks)} archive entries but chose {len(sources)} source files. Select the same number of entries as source files, or clear the row selection to map files to the first entries in archive order.')
                mapping_note = 'Mapping: selected archive rows, in physical archive order.'
            else:
                if len(sources) > len(replaceable):
                    raise ValueError(f'You chose {len(sources)} audio files, but only {len(replaceable)} supported audio entries were detected in this PAK.')
                target_tracks = replaceable[:len(sources)]
                mapping_note = f'No archive rows were selected, so files will replace the first {len(sources)} supported entries in physical archive order. To target specific entries, select exactly {len(sources)} rows before starting.'

            plans = []
            for source, tr in zip(sources, target_tracks):
                if not source.is_file():
                    raise ValueError(f'Source file not found: {source}')
                idx = tr.record.index
                if tr.has_stream_header:
                    destination = folder / f'entry_{idx:04d}.bin'
                    header_path = folder / f'entry_{idx:04d}.hdr'
                    if not destination.is_file() or not header_path.is_file():
                        raise ValueError(f'Missing extracted files for entry {idx:04d}. Re-extract the selected PAK before replacing it.')
                    header = header_path.read_bytes()
                    if len(header) != core.GTASA_STREAM_HEADER_SIZE or core.gta_sa_stream_header(header) is None:
                        raise ValueError(f'{header_path.name} is not a valid GTA SA type-4 header. Re-extract the archive and try again.')
                    kind = 'GTA SA dual-stream'
                elif tr.is_vag:
                    destination = folder / f'entry_{idx:04d}.vag'
                    header_path = None
                    if not destination.is_file():
                        raise ValueError(f'Missing extracted file {destination.name}. Re-extract the selected PAK before replacing it.')
                    core.read_vag_size(destination)
                    kind = 'VAGp'
                else:
                    continue
                plans.append({'source':source, 'track':tr, 'destination':destination, 'header_path':header_path, 'kind':kind})
            if len(plans) != len(sources):
                raise ValueError('Not every chosen source has a supported destination. No files were changed.')
            preview = '\n'.join(f'entry_{item["track"].record.index:04d}  ←  {item["source"].name}  ({item["kind"]})' for item in plans[:18])
            if len(plans) > 18:
                preview += f'\n… and {len(plans)-18} additional mapping(s)'
            prompt = (f'Prepare {len(plans)} replacement(s) for {Path(pak).name}?\n\n{mapping_note}\n\n'
                      f'{preview}\n\nEach source is decoded/resampled, encoded as PS2 SPU ADPCM, and then packed into the correct GTA SA PS2 stream layout. '
                      'Type-4 entries get both mini/full streams and updated stream-size/rate fields. The extracted files will be changed only after each song converts successfully; the original PAK/lookup files will not be overwritten.\n\nContinue?')
            if not messagebox.askyesno('Confirm batch replacement', prompt):
                return
            self.busy = True
            self.radio_progress['value'] = 0
            self.radio_progress_pct.set('0%')
            self.radio_progress_text.set(f'Preparing 0/{len(plans)} replacements…')
            self.status.set(f'Batch replacement starting for {len(plans)} song(s)…')
            total = len(plans)
            report_path = folder / 'batch_replace_report.txt'

            def worker():
                successes = []
                failures = []
                report_lines = [f'GTA SA PS2 batch replacement report', f'Source PAK: {pak}', f'TRAKLKUP.DAT: {lookup}', f'Archive ID: 0x{gid:02X}', f'Offset unit: {off_unit}', f'Size unit: {size_unit}', '']
                for i, item in enumerate(plans):
                    source = item['source']; tr = item['track']; idx = tr.record.index
                    dest = item['destination']; hdr_path = item['header_path']; kind = item['kind']
                    stage_count = 2 if tr.has_stream_header else 1
                    def put_progress(stage_text, frac=0.0, file_index=i, src_name=source.name):
                        value = 100.0 * (file_index + max(0.0, min(1.0, frac))) / max(1, total)
                        self.q.put(('batch_replace_progress', {'value': value, 'text': f'[{file_index+1}/{total}] {src_name}: {stage_text}'}))
                    try:
                        with tempfile.TemporaryDirectory(prefix='.gta_batch_replace_', dir=str(folder)) as td:
                            work = Path(td)
                            rate = tr.sample_rate if tr.sample_rate and 8000 <= tr.sample_rate <= 48000 else core.PS2_RADIO_SAMPLE_RATE
                            def callback_factory(stage_num, label):
                                def callback(message):
                                    subpct = 0.02
                                    if message.startswith('Encoding '):
                                        try: subpct = max(0.0, min(1.0, float(message.split()[1].rstrip('%')) / 100.0))
                                        except Exception: subpct = 0.02
                                    elif message.startswith('Decoding'):
                                        subpct = 0.01
                                    put_progress(f'{label}: {message}', (stage_num + subpct) / stage_count)
                                return callback
                            full_raw = work / 'full_rate.adpcm'
                            audio.convert_audio_to_ps2_payload(source, full_raw, ff, rate, progress=callback_factory(0, 'Full-rate stream'))
                            if tr.has_stream_header:
                                mini_raw = work / 'mini_rate.adpcm'
                                audio.convert_audio_to_ps2_payload(source, mini_raw, ff, audio.GTASA_MINI_RATE, progress=callback_factory(1, 'Mini stream'))
                                packed = work / 'replacement_payload.bin'
                                meta = audio.pack_gtasa_ps2_stream_payload(full_raw, mini_raw, packed)
                                if packed.stat().st_size != meta['packed_bytes'] or packed.stat().st_size % audio.GTASA_CHUNK_SIZE:
                                    raise ValueError('The GTA SA packed payload did not pass chunk-size validation.')
                                old_header = hdr_path.read_bytes()
                                new_header = audio.update_gtasa_type4_stream_header(old_header, meta, rate)
                                stream_check = audio.validate_gtasa_type4_stream(new_header, packed, expected_full_rate=rate)
                                if core.gta_sa_stream_header(new_header) is None:
                                    raise ValueError('The converted stream metadata failed GTA SA type-4 validation.')
                                ready_payload = work / 'entry_ready.bin'
                                ready_header = work / 'entry_ready.hdr'
                                shutil.copyfile(packed, ready_payload)
                                ready_header.write_bytes(new_header)
                                # Both files are fully staged and validated before committing them.
                                os.replace(ready_payload, dest)
                                os.replace(ready_header, hdr_path)
                                detail = f'{dest.name}: {packed.stat().st_size:,} payload bytes, {stream_check["chunk_count"]} validated chunk(s), {rate} Hz full + {audio.GTASA_MINI_RATE} Hz mini, ADPCM start/end markers OK'
                            else:
                                vag = work / 'replacement.vag'
                                audio.wrap_vagp(full_raw, vag, rate)
                                parsed = core.read_vag_size(vag)
                                if parsed != vag.stat().st_size:
                                    raise ValueError('The generated VAGp wrapper length did not pass validation.')
                                ready_vag = work / 'entry_ready.vag'
                                shutil.copyfile(vag, ready_vag)
                                os.replace(ready_vag, dest)
                                detail = f'{dest.name}: {vag.stat().st_size:,} bytes VAGp at {rate} Hz'
                        successes.append({'record':idx,'source':source.name,'destination':dest.name,'detail':detail})
                        report_lines.append(f'OK | entry {idx:04d} | {source} | {detail}')
                    except Exception as exc:
                        error = str(exc)
                        failures.append({'record':idx,'source':source.name,'destination':dest.name,'error':error})
                        report_lines.append(f'FAIL | entry {idx:04d} | {source} | {error}')
                    value = 100.0 * (i + 1) / max(1, total)
                    self.q.put(('batch_replace_progress', {'value': value, 'text': f'Finished {i+1}/{total}: {source.name}'}))
                try:
                    report_path.write_text('\n'.join(report_lines) + '\n', encoding='utf-8')
                except Exception as exc:
                    report_lines.append(f'Could not write report: {exc}')
                self.q.put(('batch_replace_done', {'successes':successes, 'failures':failures, 'total':total, 'report_path':str(report_path)}))
            threading.Thread(target=worker, daemon=True).start()
        except Exception as exc:
            messagebox.showerror('Batch replacement failed', str(exc))

    @staticmethod
    def _natural_audio_key(value):
        return [(0, int(bit)) if bit.isdigit() else (1, bit.casefold()) for bit in re.split(r'(\d+)', value)]

    def rebuild_radio(self):
        if self._selected_game_profile()[1]['mode']!='sa_pak':
            messagebox.showinfo('Rebuild Archive','Only GTA San Andreas PS2 uses the PAK + TRAKLKUP.DAT archive rebuild workflow. The selected game uses a single prepared radio asset.')
            return
        try:
            pak,lookup,g,o,s=self._radio_args(); folder=self.extract_dir.get().strip(); out=self.out_dir.get().strip()
            if not folder: raise ValueError('Choose the extracted tracks folder first.')
            if not out: out=filedialog.askdirectory(title='Choose output folder for rebuilt PAK and lookup table')
            if not out:return
            pakout,lkout,count,ou,su=core.rebuild_archive(pak,lookup,Path(folder),Path(out),g,o,s)
            self.last_rebuild_pak=Path(pakout); self.last_rebuild_lookup=Path(lkout)
            report_path=Path(pakout).parent/'rebuild_report.json'
            report={}
            if report_path.is_file():
                report=json.loads(report_path.read_text(encoding='utf-8'))
            rows=report.get('table_records_updated',[])
            offset_unit=report.get('offset_unit',ou); size_unit=report.get('size_unit',su)
            offset_values=[]; size_values=[]; sample_rows=[]
            for row in rows:
                if len(row)<3: continue
                record_index,offset_raw,size_raw=row[:3]
                offset_bytes=core.offset_to_bytes(int(offset_raw),offset_unit)
                size_bytes=core.size_to_bytes(int(size_raw),size_unit)
                offset_values.append(offset_bytes); size_values.append(size_bytes)
                if len(sample_rows)<5:
                    sample_rows.append(f'#{int(record_index):04d}: offset {offset_bytes:,} B (0x{offset_bytes:X}), payload {size_bytes:,} B')
            roundtrip=report.get('roundtrip_verification',{})
            verified=bool(roundtrip.get('passed',False))
            checked=int(roundtrip.get('entries_compared',count))
            offset_summary=(f'Numeric PAK offsets: {min(offset_values):,}–{max(offset_values):,} bytes '
                            f'(0x{min(offset_values):X}–0x{max(offset_values):X})') if offset_values else 'Numeric PAK offsets: see rebuild_report.json'
            size_summary=(f'Numeric payload sizes: {min(size_values):,}–{max(size_values):,} bytes '
                          f'(lookup-size values converted from {size_unit})') if size_values else 'Numeric payload sizes: see rebuild_report.json'
            details=[f'Rebuilt entries: {count:,}',
                     f'Round-trip verification: {"PASSED" if verified else "NOT CONFIRMED"} ({checked:,} entries checked)',
                     f'PAK file size: {Path(pakout).stat().st_size:,} bytes',
                     f'Lookup table size: {Path(lkout).stat().st_size:,} bytes',
                     f'Lookup units: offsets stored as {offset_unit}; payload sizes stored as {size_unit}',
                     offset_summary,size_summary,'',
                     f'PAK: {pakout}',f'Lookup: {lkout}',
                     f'Detailed report: {report_path}']
            if sample_rows:
                details.extend(['','Sample updated lookup records:']+sample_rows)
            details.extend(['','Next: click Prepare Files for ISO Patching, then Patch ISO to create a separate test image. Open that patched image in PCSX2 to validate playback.'])
            self.status.set(f'Rebuilt {count} entries; numeric offsets and payload sizes are in the completion report.')
            messagebox.showinfo('Rebuild complete','\n'.join(details))
        except Exception as e: messagebox.showerror('Rebuild failed',str(e))

    def patch_iso(self):
        """Patch a prepared radio replacement for the selected game/platform."""
        game_id,profile=self._selected_game_profile()
        if profile['mode']=='research_required':
            messagebox.showwarning('Radio target not verified',profile.get('research_note','This game’s radio patch target has not been verified.'))
            return
        if profile['mode']=='direct':
            return self._patch_direct_game_iso(game_id,profile)
        if self.busy:
            messagebox.showwarning('Operation in progress', 'Wait for the current conversion or file operation to finish before patching an ISO.')
            return
        try:
            staging_root=self.last_prepared_root
            staging_info=None
            if staging_root is not None:
                try:
                    staging_info=iso_patcher.inspect_staging(staging_root)
                except Exception:
                    staging_info=None
            if staging_info is None:
                manifest=filedialog.askopenfilename(
                    title='Select ISO_PATCH_MANIFEST.json from Prepare Files for ISO Patching',
                    initialdir=str(Path.cwd()),
                    filetypes=[('ISO patch staging manifest','ISO_PATCH_MANIFEST.json'),('JSON files','*.json'),('All files','*.*')]
                )
                if not manifest:
                    return
                staging_root=Path(manifest).expanduser().resolve().parent
                staging_info=iso_patcher.inspect_staging(staging_root)
                self.last_prepared_root=staging_root
                self.last_prepared_game_id='gta_sa_ps2'

            iso_text=filedialog.askopenfilename(
                title='Select GTA San Andreas PS2 source image',
                initialdir=str(Path.cwd()),
                filetypes=[('PS2 images (ISO, CSO, ISZ)','*.iso *.cso *.isz'),('ISO image','*.iso'),('Compressed ISO (CSO)','*.cso'),('Compressed ISO (ISZ)','*.isz'),('All files','*.*')]
            )
            if not iso_text:
                return
            iso_path=Path(iso_text).expanduser().resolve()
            default_ext = iso_path.suffix.lower() if iso_path.suffix.lower() in ('.cso', '.isz') else '.iso'
            default_name=f'{iso_path.stem}_patched_{staging_info["archive_name"].replace(".PAK", "")}{default_ext}'
            output_text=filedialog.asksaveasfilename(
                title='Save patched image as (source image is never overwritten)',
                initialdir=str(iso_path.parent), initialfile=default_name,
                defaultextension=default_ext, filetypes=[('ISO image (*.iso)','*.iso'),('Compressed CSO image (*.cso)','*.cso'),('Compressed ISZ image (*.isz)','*.isz')]
            )
            if not output_text:
                return
            output_path=Path(output_text).expanduser().resolve()
            if output_path == iso_path:
                raise ValueError('The output path is the same as the source image. Choose a different output filename.')
            overwrite=False
            if output_path.exists():
                if not messagebox.askyesno('Replace existing output?',
                    f'This output file already exists:\n{output_path}\n\nReplace it only after the new image has been written and verified?\nThe source ISO will never be modified.'):
                    return
                overwrite=True
            needed=iso_path.stat().st_size
            input_format = iso_path.suffix.lower()
            if input_format == '.cso':
                conversion_note = 'The CSO will be decompressed to a temporary ISO before patching.'
            elif input_format == '.isz':
                conversion_note = 'The ISZ will be decompressed to a temporary ISO before patching. Encrypted/password-protected ISZ images are not supported.'
            else:
                conversion_note = 'The ISO will be patched directly.'
            output_note = ('The patched ISO will then be recompressed to CSO and round-trip verified.'
                           if output_path.suffix.lower() == '.cso' else
                           'The patched ISO will be compressed into unencrypted ISZ v1 and verified against its original SHA-256.'
                           if output_path.suffix.lower() == '.isz' else
                           'The final output will be a plain ISO image.')
            preview=(
                'Create a patched image using the prepared radio files?\n\n'
                f'STATION: {staging_info["archive_name"]} (ID 0x{staging_info["archive_id"]:02X})\n'
                f'INPUT IMAGE: {iso_path}\nOUTPUT IMAGE: {output_path}\n\n'
                f'PAK: {staging_info["pak_path"]}\nLOOKUP: {staging_info["lookup_path"]}\n\n'
                'The app will verify the San Andreas PS2 disc serial, locate both game-disc paths, patch a separate image copy, '
                'and hash-verify the final file contents.\n'
                f'Ensure enough free disk space for the decompressed and patched temporary image(s) (source file is {needed:,} bytes).\n'
                f'{conversion_note}\n{output_note}\n\n'
                'The source image will not be modified. Actual audio playback must still be tested in PCSX2.'
            )
            if not messagebox.askyesno('Patch GTA San Andreas PS2 ISO',preview):
                return
            self.busy=True
            self.game_combo.configure(state='disabled')
            self.station_combo.configure(state='disabled')
            self.patch_iso_button.configure(state='disabled')
            self.status.set('Starting ISO patch…')
            stage=Path(staging_info['root'])
            def worker():
                try:
                    result=iso_patcher.patch_sa_ps2_radio_image(
                        iso_path, output_path, stage,
                        progress_callback=lambda text: self.q.put(('iso_patch_progress',text)),
                        overwrite=overwrite
                    )
                    self.q.put(('iso_patch_done',result))
                except Exception as exc:
                    self.q.put(('iso_patch_error',str(exc)))
            threading.Thread(target=worker,daemon=True).start()
        except Exception as exc:
            self.busy=False
            try:
                self.game_combo.configure(state='readonly')
                self._refresh_station_options()
                self.patch_iso_button.configure(state='normal')
            except Exception:
                pass
            messagebox.showerror('Patch ISO failed',str(exc))

    def _patch_direct_game_iso(self,game_id,profile):
        if self.busy:
            messagebox.showwarning('Operation in progress','Wait for the current operation to finish before patching an image.')
            return
        try:
            staging_root=self.last_prepared_root
            staging_info=None
            if staging_root is not None and self.last_prepared_game_id==game_id and self.last_prepared_station_id==profile.get('station_id'):
                try: staging_info=iso_patcher.inspect_game_radio_staging(staging_root,game_id,profile.get('station_id'))
                except Exception: staging_info=None
            if staging_info is None:
                manifest=filedialog.askopenfilename(title=f"Select prepared manifest for {profile['title']}",initialdir=str(Path.cwd()),filetypes=[('ISO patch staging manifest','ISO_PATCH_MANIFEST.json'),('JSON files','*.json'),('All files','*.*')])
                if not manifest:return
                staging_root=Path(manifest).expanduser().resolve().parent
                staging_info=iso_patcher.inspect_game_radio_staging(staging_root,game_id,profile.get('station_id'))
                self.last_prepared_root=staging_root; self.last_prepared_game_id=game_id; self.last_prepared_station_id=profile.get('station_id')
            iso_text=filedialog.askopenfilename(title=f"Select {profile['title']} source image",initialdir=str(Path.cwd()),filetypes=[('ISO / CSO / ISZ images','*.iso *.cso *.isz'),('ISO image','*.iso'),('Compressed CSO image','*.cso'),('Compressed ISZ image','*.isz'),('All files','*.*')])
            if not iso_text:return
            iso_path=Path(iso_text).expanduser().resolve()
            default_ext=iso_path.suffix.lower() if iso_path.suffix.lower() in ('.cso','.isz') else '.iso'
            default_name=f"{iso_path.stem}_patched_{game_id}_{profile.get('station_id') or profile['asset_filename'].split('.')[0].lower()}{default_ext}"
            output_text=filedialog.asksaveasfilename(title='Save patched image as (source image is never overwritten)',initialdir=str(iso_path.parent),initialfile=default_name,defaultextension=default_ext,filetypes=[('ISO image (*.iso)','*.iso'),('Compressed CSO image (*.cso)','*.cso'),('Compressed ISZ image (*.isz)','*.isz')])
            if not output_text:return
            output_path=Path(output_text).expanduser().resolve()
            if output_path==iso_path:raise ValueError('The output path is the same as the source image. Choose a different filename.')
            overwrite=False
            if output_path.exists():
                if not messagebox.askyesno('Replace existing output?',f'This output already exists:\n{output_path}\n\nReplace it only after the new image has been verified? The source image will never be modified.'):return
                overwrite=True
            input_format=iso_path.suffix.lower()
            conversion_note=('The CSO will be decompressed to a temporary ISO.' if input_format=='.cso' else 'The ISZ will be decompressed to a temporary ISO; encrypted/password-protected ISZ is not supported.' if input_format=='.isz' else 'The ISO will be patched directly.')
            output_note=('The result will be recompressed to CSO and round-trip SHA-256 verified.' if output_path.suffix.lower()=='.cso' else 'The result will be compressed natively to unencrypted ISZ and round-trip SHA-256 verified.' if output_path.suffix.lower()=='.isz' else 'The final output will be a plain ISO.')
            companion_note=''
            if profile.get('header_companion_path'):
                slot_offset=int(profile.get('header_slot_index',0))*int(profile.get('header_block_size',0x800))
                companion_note=(f"\n\nCOMPANION CACHE: {profile['header_companion_path']}\n"
                                f"The selected 0x800-byte slot {profile['header_slot_index']} (offset 0x{slot_offset:X}) will be updated from the replacement stream prefix and verified after patching.")
            if profile.get('preserve_dependency_path'):
                companion_note+=(f"\nDEPENDENCY PRESERVED: {profile['preserve_dependency_path']} will remain byte-for-byte unchanged because its internal records are not decoded yet.")
            profile_warning=profile.get('patch_warning','')
            preview=(f"Patch a radio stream for {profile['title']}?\n\nSTATION: {profile['station']}\n"
                     f"TARGET: {profile['target_path']}\nASSET: {staging_info['payload_path']} ({staging_info['size_bytes']:,} bytes)\n"
                     f"INPUT IMAGE: {iso_path}\nOUTPUT IMAGE: {output_path}\n\n"
                     f"The app will verify {profile['identity_file']} against known {profile['platform']} disc IDs, patch only the selected game's target path, and verify the output stream hash.\n"
                     f"{conversion_note}\n{output_note}{companion_note}\n\nThe source image will not be modified. The selected radio file must already be correctly encoded for the target game."
                     + (f"\n\nWARNING: {profile_warning}" if profile_warning else ""))
            if not messagebox.askyesno(f"Patch {profile['title']}",preview):return
            self.busy=True; self.game_combo.configure(state='disabled'); self.station_combo.configure(state='disabled'); self.patch_iso_button.configure(state='disabled'); self.status.set('Starting game-aware ISO patch…')
            stage=Path(staging_info['root'])
            def worker():
                try:
                    result=iso_patcher.patch_game_radio_image(iso_path,output_path,stage,game_id,progress_callback=lambda text:self.q.put(('iso_patch_progress',text)),overwrite=overwrite,station_id=profile.get('station_id'))
                    self.q.put(('iso_patch_done',result))
                except Exception as exc:self.q.put(('iso_patch_error',str(exc)))
            threading.Thread(target=worker,daemon=True).start()
        except Exception as exc:
            self.busy=False
            try:
                self.game_combo.configure(state='readonly'); self._refresh_station_options(); self.patch_iso_button.configure(state='normal')
            except Exception:pass
            messagebox.showerror('Patch ISO failed',str(exc))

    def prepare_files_for_iso_patching(self):
        """Prepare files for the selected platform without modifying an ISO."""
        game_id,profile=self._selected_game_profile()
        if profile['mode']=='research_required':
            messagebox.showwarning('Radio target not verified',profile.get('research_note','This game’s radio patch target has not been verified.'))
            return
        if profile['mode']=='direct':
            try: return self.prepare_direct_radio_file(game_id,profile)
            except Exception as exc: messagebox.showerror('Prepare Radio File failed',str(exc)); return
        try:
            pak_path=self.last_rebuild_pak
            lookup_path=self.last_rebuild_lookup
            if not (pak_path and pak_path.is_file() and lookup_path and lookup_path.is_file()):
                initial=Path(self.out_dir.get().strip()).expanduser() if self.out_dir.get().strip() else Path.cwd()
                chosen=filedialog.askopenfilename(
                    title='Select rebuilt radio PAK (NEW-*.PAK)',initialdir=str(initial),
                    filetypes=[('Rebuilt radio PAK','NEW-*.PAK'),('PAK archive','*.PAK'),('All files','*.*')]
                )
                if not chosen:return
                pak_path=Path(chosen)
                sibling=pak_path.parent/'NEW-TRAKLKUP.DAT'
                if sibling.is_file():
                    lookup_path=sibling
                else:
                    chosen_lookup=filedialog.askopenfilename(
                        title='Select the matching NEW-TRAKLKUP.DAT',initialdir=str(pak_path.parent),
                        filetypes=[('Rebuilt lookup table','NEW-TRAKLKUP.DAT'),('DAT files','*.DAT'),('All files','*.*')]
                    )
                    if not chosen_lookup:return
                    lookup_path=Path(chosen_lookup)
            if not pak_path.is_file() or not lookup_path.is_file():
                raise ValueError('The previously rebuilt PAK/lookup pair is no longer available. Rebuild again or select the files manually.')
            initial_out=Path(self.out_dir.get().strip()).expanduser() if self.out_dir.get().strip() else pak_path.parent
            destination=filedialog.askdirectory(title='Choose staging root (the app creates AUDIO/STREAMS and AUDIO/CONFIG)',initialdir=str(initial_out))
            if not destination:return
            destination_path=Path(destination)
            canonical=pak_path.name[4:].upper() if pak_path.name.upper().startswith('NEW-') else pak_path.name.upper()
            preview=(f'Prepare the rebuilt station files for the Patch ISO workflow?\n\n'
                     f'INPUT PAK: {pak_path.name}\n'
                     f'INPUT LOOKUP: {lookup_path.name}\n\n'
                     'STAGED GAME PATHS:\n'
                     f'PAK → {destination_path / "AUDIO" / "STREAMS" / canonical}\n'
                     f'Lookup → {destination_path / "AUDIO" / "CONFIG" / "TRAKLKUP.DAT"}\n\n'
                     'The NEW- prefix is removed from the staged filenames where applicable.\n'
                     'A manifest with sizes and SHA-256 hashes will be written.\n'
                     'This preparation step does not modify an ISO. Click Patch ISO afterwards to create a patched copy.')
            if not messagebox.askyesno('Prepare Files for ISO Patching',preview):return
            try:
                result=core.prepare_files_for_iso_patching(pak_path,lookup_path,destination_path,overwrite=False)
            except core.ExistingStagingFilesError as conflict:
                if not messagebox.askyesno('Staging files already exist',str(conflict)+'\n\nContinue and overwrite these exact outputs? No backup files will be created.'):
                    return
                result=core.prepare_files_for_iso_patching(pak_path,lookup_path,destination_path,overwrite=True)
            self.last_prepared_root=destination_path; self.last_prepared_game_id='gta_sa_ps2'
            self.status.set(f'Prepared {result["archive_name"]} and TRAKLKUP.DAT for ISO patching.')
            messagebox.showinfo('Files prepared for ISO patching',
                f'Archive: {result["archive_name"]} (ID 0x{result["archive_id"]:02X})\n'
                f'Track records inspected: {result["tracks_inspected"]:,}\n'
                f'PAK size: {result["pak_size_bytes"]:,} bytes\n'
                f'Lookup size: {result["lookup_size_bytes"]:,} bytes\n'
                f'Numeric offset range: {result["offset_bytes_min"]:,}–{result["offset_bytes_max"]:,} bytes\n'
                f'Numeric payload-size range: {result["payload_bytes_min"]:,}–{result["payload_bytes_max"]:,} bytes\n\n'
                f'PAK: {result["pak_path"]}\nLookup: {result["lookup_path"]}\n'
                f'Manifest: {result["manifest_path"]}\n\n'
                'Next: click Patch ISO to create a separate patched test image, then run that image in PCSX2. The original source ISO will remain unchanged.')
        except Exception as e:
            messagebox.showerror('Prepare Files for ISO Patching failed',str(e))
    def add_audio(self):
        paths=filedialog.askopenfilenames(title='Add audio sources',filetypes=[('Audio files','*.wav *.mp3 *.ogg *.flac *.m4a *.aac *.wma *.aiff'),('All files','*.*')])
        self.queue_audio_paths(paths)
    def drop_audio(self,event):
        try: paths=self.tk.splitlist(event.data); self.queue_audio_paths(paths)
        except Exception as exc: messagebox.showerror('Audio drop failed',str(exc))
    def queue_audio_paths(self,paths):
        for value in paths:
            p=Path(value)
            if not p.is_file(): continue
            iid=str(max([int(x) for x in self.audio_queue.keys()] + [-1]) + 1); self.audio_queue[iid]=p; self.audio_tree.insert('','end',iid=iid,values=(str(p),human(p.stat().st_size),'Queued'))
    def remove_audio(self):
        selected=self.audio_tree.selection()
        for iid in selected:
            self.audio_tree.delete(iid); self.audio_queue.pop(iid,None)
    def clear_audio(self): self.audio_queue={}; self.audio_tree.delete(*self.audio_tree.get_children()); self.audio_progress['value']=0; self.audio_pct.set('0%')
    def convert_audio(self):
        if self.busy: return
        files=[(iid,self.audio_queue[iid]) for iid in self.audio_tree.get_children() if iid in self.audio_queue]
        if not files: messagebox.showinfo('Audio conversion','Add one or more audio files first.'); return
        out=filedialog.askdirectory(title='Choose output folder for converted audio')
        if not out:return
        ff=self.ffmpeg.get().strip() or shutil.which('ffmpeg')
        if not ff: messagebox.showerror('FFmpeg required','Install FFmpeg or select ffmpeg.exe.'); return
        output_dir=Path(out)
        try: output_dir.mkdir(parents=True,exist_ok=True)
        except Exception as exc: messagebox.showerror('Output folder',str(exc)); return
        profile_name=self.audio_format.get();profile=audio_profiles.PROFILES[profile_name];ext=profile['ext']
        # Reserve non-colliding names up-front. Files with identical stems must never
        # overwrite one another when a mixed folder is converted as a batch.
        destinations=[]; reserved=set()
        for _,src in files:
            stem=src.stem; candidate=stem+ext; n=2
            while candidate.casefold() in reserved or (output_dir/candidate).exists():
                candidate=f'{stem}_{n:02d}{ext}'; n+=1
            reserved.add(candidate.casefold()); destinations.append(output_dir/candidate)
        self.busy=True; self.audio_progress['value']=0; self.audio_pct.set('0%')
        total=len(files)
        def worker():
            successes=[]; failures=[]
            for i,((iid,src),dest) in enumerate(zip(files,destinations)):
                self.q.put(('audio_row',{'iid':iid,'state':'Starting…'}))
                self.q.put(('status',f'Converting {i+1}/{total}: {src.name}'))
                try:
                    # Stage under the output folder so the final rename is atomic and
                    # a failed FFmpeg/ADPCM pass cannot leave a partial destination.
                    with tempfile.TemporaryDirectory(prefix='.gta_convert_',dir=str(output_dir)) as td:
                        stage=Path(td); raw=stage/'audio.adpcm'
                        def stage_progress(message, index=i, filename=src.name):
                            self.q.put(('status',f'[{index+1}/{total}] {filename}: {message}'))
                            if message.startswith('Encoding '):
                                try: pct=float(message.partition(' ')[2].rstrip('%'))
                                except ValueError: return
                                overall=100.0*(index+max(0,min(100,pct))/100.0)/total
                                self.q.put(('audio_progress',overall))
                        ready=stage/('ready_output'+ext)
                        result=audio_profiles.convert(src,ready,profile_name,ff,self.audio_at3tool.get().strip(),self.audio_reference_at3.get().strip(),progress=stage_progress,station_name=(self.audio_ps2_station.get().strip() or src.stem) if profile['platform']=='PS2' else '',sample_rate_override=(None if self.audio_ps2_rate_override.get().startswith('Auto') else int(self.audio_ps2_rate_override.get().split()[0])) if profile['platform']=='PS2' else None)
                        os.replace(ready,dest)
                    successes.append((dest,result))
                    rate_note = f" · {result['sample_rate']} Hz · {result['vb_estimated_duration_seconds']/60:.2f} min" if profile['platform']=='PS2' else f" · {result['duration_seconds']/60:.2f} min"
                    self.q.put(('audio_row',{'iid':iid,'state':f'Complete · {human(dest.stat().st_size)}{rate_note} · {dest.name}'}))
                except Exception as exc:
                    failures.append((src,str(exc)))
                    self.q.put(('audio_row',{'iid':iid,'state':f'Failed · {str(exc)[:100]}'}))
                self.q.put(('audio_progress',100.0*(i+1)/total))
            lines=[f'Finished batch: {len(successes)} succeeded, {len(failures)} failed out of {total}.',f'Output folder: {output_dir}']
            for dest, meta in successes:
                if profile['platform']=='PS2':
                    layout_note = ("encoded L/R channel blocks identical (dual-mono layout)" if meta.get('encoded_channels_identical') else "encoded L/R channels contain differences (stereo layout)")
                    lines.append(f"{dest.name}: source {meta['source_duration_seconds']:.2f}s → VB {meta['vb_estimated_duration_seconds']:.2f}s; {meta['sample_rate']} Hz, channel interleave 0x2000 (padding +{meta['padding_duration_seconds']:.2f}s); {layout_note}.")
                else:
                    lines.append(f"{dest.name}: ATRAC3plus, declared duration {meta['duration_seconds']:.2f}s (runtime playback not tested).")
            if failures:
                lines.append('')
                lines.extend(f'{src.name}: {err[:240]}' for src,err in failures[:8])
                if len(failures)>8: lines.append(f'… plus {len(failures)-8} additional failure(s).')
            self.q.put(('audio_done','\n'.join(lines)))
        threading.Thread(target=worker,daemon=True).start()
    def _legacy_open_txd(self):
        p=filedialog.askopenfilename(title='Open RenderWare TXD',filetypes=[('TXD dictionaries','*.txd'),('All files','*.*')]);
        if p:self.txd_path_var.set(p); self.inspect_txd()
    def _legacy_inspect_txd(self):
        p=self.txd_path_var.get().strip()
        if not p: p=filedialog.askopenfilename(title='Open RenderWare TXD',filetypes=[('TXD dictionaries','*.txd'),('All files','*.*')]); self.txd_path_var.set(p)
        if not p:return
        try:
            data=Path(p).read_bytes(); chunks=parse_rw_chunks(data); self.txd_data=data; self.txd_chunks=chunks; self.txd_tree.delete(*self.txd_tree.get_children())
            for i,(off,depth,name,size,details) in enumerate(chunks): self.txd_tree.insert('','end',iid=str(i),values=(f'0x{off:08X}','  '*depth+name,human(size),details))
            self.status.set(f'Parsed {len(chunks)} RenderWare chunks from {Path(p).name}.')
        except Exception as e:messagebox.showerror('TXD inspection failed',str(e))
    def export_txd_chunk(self):
        if self.txd_data is None: messagebox.showinfo('TXD','Open a TXD first.'); return
        sel=self.txd_tree.selection()
        if not sel: messagebox.showinfo('TXD','Select a chunk row first.'); return
        off,depth,name,size,detail=self.txd_chunks[int(sel[0])]; start=off; end=min(len(self.txd_data),off+12+size)
        out=filedialog.asksaveasfilename(title='Export original chunk bytes',initialfile=f'{name.replace(" ","_")}_{off:08X}.bin',defaultextension='.bin');
        if out: Path(out).write_bytes(self.txd_data[start:end]); self.status.set(f'Exported original chunk bytes to {out}.')
    def open_gxt(self):
        p=filedialog.askopenfilename(title='Open GTA GXT',filetypes=[('GXT text resources','*.gxt'),('All files','*.*')]);
        if not p:return
        self.gxt_path_var.set(p)
        try:
            self.gxt_doc=GXTDocument(Path(p)); self.gxt_path=Path(p); self.refresh_gxt(); self.gxt_info.set(f'{len(self.gxt_doc.tables)} tables · {len(self.gxt_doc.entries)} strings · encoding {self.gxt_doc.encoding} · offset-preserving edit mode')
            self.status.set(f'Loaded GXT {Path(p).name}.')
        except Exception as e:self.gxt_doc=None; messagebox.showerror('GXT parse failed',str(e))
    def refresh_gxt(self):
        self.gxt_tree.delete(*self.gxt_tree.get_children()); self.gxt_visible=[]
        if not self.gxt_doc:return
        filt=self.gxt_filter.get().casefold() if hasattr(self,'gxt_filter') else ''
        for i,item in enumerate(self.gxt_doc.entries):
            hay=f'{item["table"]} {item["key"]} {item["text"]}'.casefold()
            if filt and filt not in hay:continue
            iid=str(len(self.gxt_visible)); self.gxt_visible.append(item); self.gxt_tree.insert('','end',iid=iid,values=(item['table'],item['key'],item['text'].replace('\n','↵'),item['slot']))
    def edit_gxt(self):
        sel=self.gxt_tree.selection()
        if not self.gxt_doc or not sel:messagebox.showinfo('GXT editor','Open a GXT and select a string.'); return
        item=self.gxt_visible[int(sel[0])]; win=tk.Toplevel(self); win.title(f'Edit {item["table"]} / {item["key"]}'); win.geometry('620x300'); win.configure(bg=BG)
        ttk.Label(win,text=f'Table: {item["table"]}    Key: {item["key"]}    Slot: {item["slot"]} bytes').pack(anchor='w',padx=12,pady=8)
        ttk.Label(win,text='Current / replacement text').pack(anchor='w',padx=12); box=tk.Text(win,height=7,bg='#0b1220',fg=FG,insertbackground='white',wrap='word'); box.pack(fill='both',expand=True,padx=12,pady=5); box.insert('1.0',item['text'])
        def apply():
            try:self.gxt_doc.replace(item,box.get('1.0','end-1c')); self.refresh_gxt(); win.destroy(); self.status.set('String edited in memory; choose Save edits as… to write a copy.')
            except Exception as e:messagebox.showerror('Cannot apply edit',str(e),parent=win)
        ttk.Button(win,text='Apply edit',style='Accent.TButton',command=apply).pack(anchor='e',padx=12,pady=8)
    def save_gxt(self):
        if not self.gxt_doc:messagebox.showinfo('GXT editor','Open a GXT first.'); return
        out=filedialog.asksaveasfilename(title='Save edited GXT as',initialfile=(self.gxt_path.stem+'_edited.gxt' if self.gxt_path else 'edited.gxt'),defaultextension='.gxt',filetypes=[('GXT files','*.gxt'),('All files','*.*')]);
        if out:
            try:self.gxt_doc.save(out); self.status.set(f'Saved edited GXT: {out}'); messagebox.showinfo('GXT saved',f'Saved edited copy:\n{out}\n\nOffsets and file length were preserved.')
            except Exception as e:messagebox.showerror('Save failed',str(e))
    def _legacy_open_dtz(self):
        p=filedialog.askopenfilename(title='Open GAME.DTZ',filetypes=[('DTZ archives','*.dtz'),('All files','*.*')]);
        if p:self.dtz_path_var.set(p); self.inspect_dtz()
    def _legacy_inspect_dtz(self):
        p=self.dtz_path_var.get().strip()
        if not p:return
        try:
            raw=Path(p).read_bytes(); dec=zlib.decompress(raw); self.dtz_raw=dec; self.dtz_path=Path(p)
            sig=dec[:4]; text=f'File: {p}\nCompressed: {len(raw):,} bytes\nDecompressed: {len(dec):,} bytes\nCompression ratio: {len(raw)/max(1,len(dec)):.3f}\nDecompressed signature: {sig!r} ({sig.hex(" ")})\n\nThe whole stream decompressed successfully. The source file remains unchanged.'
            self.dtz_info.delete('1.0','end'); self.dtz_info.insert('1.0',text); self.status.set(f'Decompressed {Path(p).name}: {len(dec):,} bytes.')
        except Exception as e:self.dtz_raw=None; messagebox.showerror('DTZ inspection failed',f'Could not zlib-decompress this file:\n{e}\n\nNot every DTZ variant is a single zlib stream.')
    def _legacy_save_dtz_raw(self):
        if self.dtz_raw is None:messagebox.showinfo('DTZ tools','Inspect/decompress a DTZ first.'); return
        out=filedialog.asksaveasfilename(title='Save decompressed GAME.DAT',initialfile='GAME.DAT',defaultextension='.DAT');
        if out:Path(out).write_bytes(self.dtz_raw); self.status.set(f'Saved decompressed data to {out}.')
    def rebuild_dtz(self):
        if self.dtz_raw is None:messagebox.showinfo('DTZ tools','Inspect/decompress a DTZ first.'); return
        out=filedialog.asksaveasfilename(title='Rebuild compressed DTZ',initialfile=(self.dtz_path.stem+'_rebuilt.dtz' if self.dtz_path else 'GAME_rebuilt.DTZ'),defaultextension='.DTZ',filetypes=[('DTZ archives','*.DTZ'),('All files','*.*')]);
        if not out:return
        try:
            packed=zlib.compress(self.dtz_raw,9); check=zlib.decompress(packed)
            if check!=self.dtz_raw:raise ValueError('Round-trip verification failed.')
            Path(out).write_bytes(packed); self.status.set(f'Rebuilt DTZ and verified {len(check):,} decompressed bytes.'); messagebox.showinfo('DTZ rebuilt',f'Wrote {out}\nCompressed: {len(packed):,} bytes\nVerified decompressed payload: {len(check):,} bytes.\n\nTest in the matching game/platform before use.')
        except Exception as e:messagebox.showerror('DTZ rebuild failed',str(e))

if __name__=='__main__':
    if '--self-test' in sys.argv:
        core.run_self_test(); audio.run_gtasa_stream_layout_self_test(); audio_profiles.run_self_test(); executable_patch_core.run_self_test(); game_code_analysis.run_self_test(); PSP_EBOOT_Lab.run_self_test(); tim2_converter.run_self_test(); ref_check = executable_patch_core.verify_reference_manifest(); assert ref_check['ok'], 'Bundled patch reference verification failed: ' + '; '.join(ref_check['failures']); iso_patcher.run_self_test(); print(f'GTA Modding Studio self-tests passed, including stream/archive checks, {ref_check["verified_count"]}/{ref_check["file_count"]} patch references, LCS length-trace signature checks, PSP EBOOT scan tests, PNG-to-TIM2 conversion across all five pixel formats, texture-template mode, game-aware ISO patching, and signature-gated executable patch-copy safety.')
    else: Studio().mainloop()
