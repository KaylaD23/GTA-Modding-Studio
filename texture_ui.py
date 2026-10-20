"""Native Tk texture browsing/preview and conservative edit/save UI."""
from __future__ import annotations
from pathlib import Path
import tkinter as tk
from tkinter import filedialog,ttk,messagebox
from PIL import Image, ImageTk
from texture_formats import TXDDocument,DTZDocument,TIM2Document,png_to_tim2,png_to_tim2_template
from TM2_Replacement_Bridge import replace_txd_from_tm2, replace_dtz_from_tm2

GAMES=('GTA III PS2','GTA Vice City PS2','GTA San Andreas PS2','GTA Liberty City Stories PS2','GTA Liberty City Stories PSP','GTA Vice City Stories PS2','GTA Vice City Stories PSP')


class TextureTabMixin:
    def _texture_layout(self,parent,title,open_action,refresh_action,preview_action,export_action,replace_action,save_action,prefix):
        bar=ttk.Frame(parent);bar.pack(fill='x',pady=(0,6))
        ttk.Button(bar,text=f'Open {title}…',command=open_action).pack(side='left',padx=(0,5))
        ttk.Button(bar,text='Inspect / refresh',command=refresh_action).pack(side='left',padx=(0,5))
        ttk.Button(bar,text='Export selected PNG…',command=export_action).pack(side='left',padx=(0,5))
        ttk.Button(bar,text='Replace selected from TM2…',command=replace_action).pack(side='left',padx=(0,5))
        if prefix == 'txd':
            ttk.Button(bar,text='Modify texture…',command=lambda:self._texture_modify('txd')).pack(side='left',padx=(0,5))
        ttk.Button(bar,text=f'Save edited {title} as…',style='Accent.TButton',command=save_action).pack(side='left',padx=(0,5))
        sectionbar=ttk.Frame(parent);sectionbar.pack(fill='x',pady=(0,7))
        ttk.Label(sectionbar,text='Section').pack(side='left',padx=(0,8))
        section_var=tk.StringVar(value='All sections')
        section_combo=ttk.Combobox(sectionbar,textvariable=section_var,values=('All sections',),
                                   state='readonly',width=28)
        section_combo.pack(side='left')
        section_note=tk.StringVar(value='Select a section to filter the resource index.')
        ttk.Label(sectionbar,textvariable=section_note,style='Muted.TLabel',wraplength=530).pack(side='left',padx=12)
        for name,obj in [('section',section_var),('section_combo',section_combo),('section_note',section_note)]:
            setattr(self,f'{prefix}_{name}',obj)
        section_combo.bind('<<ComboboxSelected>>', lambda _e: self._filter_texture_section(prefix))
        row=ttk.Frame(parent);row.pack(fill='both',expand=True)
        pane=ttk.Frame(row);pane.pack(side='left',fill='both',expand=True)
        tr=ttk.Treeview(pane,columns=('dims','format','info'),show='tree headings',selectmode='browse')
        tr.heading('#0',text='Texture name');tr.column('#0',width=200,minwidth=120,stretch=True)
        for c,t,w in [('dims','Dimensions',110),('format','Pixel format',90),('info','Section / offset',220)]:tr.heading(c,text=t);tr.column(c,width=w,stretch=False)
        vs=ttk.Scrollbar(pane,orient='vertical',command=tr.yview);tr.configure(yscrollcommand=vs.set)
        tr.pack(side='left',fill='both',expand=True);vs.pack(side='left',fill='y')
        panel=ttk.Frame(row,padding=(12,5));panel.pack(side='left',fill='both',expand=False)
        preview=tk.Canvas(panel,width=310,height=310,bg='#161226',highlightthickness=1,highlightbackground='#455373')
        preview.pack(pady=(0,5))
        details=tk.StringVar(value='Open an archive, then select a texture to preview.');ttk.Label(panel,textvariable=details,wraplength=315,justify='left').pack(anchor='w')
        swizzle=tk.BooleanVar(value=True)
        ttk.Checkbutton(panel,text='Deswizzle preview (PS2/PSP; toggle to compare)',variable=swizzle,command=preview_action).pack(anchor='w',pady=(8,0))
        for name,obj in [('tree',tr),('canvas',preview),('details',details),('swizzle',swizzle)]:setattr(self,f'{prefix}_{name}',obj)
        tr.bind('<<TreeviewSelect>>',lambda ev:preview_action())
        return tr

    def _txd_ui(self):
        self.txd_doc=None;self.txd_source=None;self.txd_image_ref=None
        self._texture_layout(self.txd_tab,'TXD',self.open_txd,self.inspect_txd,self.preview_txd,self.export_txd_png,self.replace_txd_tm2,self.save_txd_copy,'txd')
        self._install_texture_context('txd')
        ttk.Label(self.txd_tab,text='Supports PS2 RenderWare indexed PAL4/PAL8 textures, plus donor-template palette-depth conversion in Modify texture. TM2 replacement writes texels and CLUT; palette-depth changes rebuild only the selected native raster. Save to a new file.',style='Muted.TLabel',wraplength=1100).pack(anchor='w',pady=(4,0))

    def _dtz_ui(self):
        self.dtz_doc=None;self.dtz_source=None;self.dtz_image_ref=None
        self._texture_layout(self.dtz_tab,'DTZ',self.open_dtz,self.inspect_dtz,self.preview_dtz,self.export_dtz_png,self.replace_dtz_tm2,self.save_dtz_copy,'dtz')
        r=ttk.Frame(self.dtz_tab);r.pack(fill='x',pady=6)
        ttk.Button(r,text='PNG → TIM2 (.tm2) converter…',command=self.open_tim2_converter).pack(side='left',padx=(0,8))
        ttk.Button(r,text='Export selected as TIM2…',command=self.export_dtz_tim2).pack(side='left',padx=(0,8))
        ttk.Button(r,text='Export decompressed GAME.DAT…',command=self.save_dtz_raw).pack(side='left',padx=(0,8))
        self.dtz_tree.bind('<<TreeviewSelect>>',lambda _e:self._on_dtz_selected())
        self._install_texture_context('dtz')
        ttk.Label(self.dtz_tab,text='GATG HUD/particle/generic textures, LCS CHK and VCS XET menu/fonts, and VCS radar. TM2 replacement checks layout and palette depth; Oversized LCS CHKs and VCS menu/fonts/radar XETs use two-table GATG relocation with indexed-pointer rebasing; in-game compatibility remains unverified. Unknown layouts remain slot-limited. Unsupported raster formats remain inspection-only.',style='Muted.TLabel',wraplength=1150).pack(anchor='w')

    def _filter_texture_section(self, prefix):
        if prefix=='dtz':
            self._populate_dtz_sections()
        else:
            self._populate_txd_sections()

    def _set_texture_sections(self, prefix, names, preferred=None):
        selector=getattr(self,prefix+'_section')
        box=getattr(self,prefix+'_section_combo')
        choices=['All sections']+sorted(set(names),key=str.casefold)
        old=preferred or selector.get()
        box.configure(values=choices)
        selector.set(old if old in choices else 'All sections')

    def _populate_txd_sections(self):
        if self.txd_doc is None:
            return
        selected=self.txd_section.get()
        self.txd_tree.delete(*self.txd_tree.get_children())
        if selected not in ('All sections','Native dictionary'):
            return
        for i,t in enumerate(self.txd_doc.textures):
            self.txd_tree.insert('','end',iid=f'tex_{i}',text=t.name,
                        values=(f'{t.width} × {t.height}',f'{t.bpp}bpp',t.note))
        self.txd_section_note.set(f'{len(self.txd_doc.textures)} editable native texture entries')
        if self.txd_doc.textures:
            self.txd_tree.selection_set('tex_0');self.preview_txd()

    def _populate_dtz_sections(self):
        if self.dtz_doc is None:
            return
        chosen=self.dtz_section.get()
        self.dtz_tree.delete(*self.dtz_tree.get_children())
        self.dtz_index_rows={}
        doc=self.dtz_doc
        texture_count=0;indexed_count=0
        available={s['name'] for s in doc.sections if not s['name'].startswith('XET@')} | set(doc.indexed_sections)
        # VCS font textures are decoded from the linked 0xFC XET stream.
        # Do not add a fake empty fonts group based only on 'fsfonts'.
        if any(section.startswith('XET@') for section,_t in doc.entries):
            available.add('radar')
        for name in sorted(available,key=str.casefold):
            if chosen not in ('All sections',name):
                continue
            entries=[(i,t) for i,(section,t) in enumerate(doc.entries)
                     if section==name or (name=='radar' and section.startswith('XET@'))]
            if name=='radar':
                entries.sort(key=lambda pair:pair[1].name.casefold())
            catalog=doc.indexed_sections.get(name,[])
            if not entries and not catalog:
                continue
            parent=''
            if chosen=='All sections':
                parent='grp_'+name
                self.dtz_tree.insert('','end',iid=parent,text=name,
                                     values=('','section',f'{len(entries)} decoded · {len(catalog)} indexed'),open=True)
            for i,t in entries:
                self.dtz_tree.insert(parent,'end',iid=f'tex_{i}',text=t.name,
                          values=(f'{t.width} × {t.height}',f'{t.bpp}bpp',f'{name}: 0x{t.pixel_offset:X}'))
                texture_count+=1
            from collections import Counter
            repeated=Counter(rec['name'] for rec in catalog)
            for i,rec in enumerate(catalog):
                iid=f'idx_{name}_{i}'
                self.dtz_index_rows[iid]=rec
                # Some VCS archives contain two separate metadata records
                # named 'footprint'; preserve both nodes and disambiguate
                # them by offset rather than silently dropping one.
                label=(f'{rec["name"]} [node 0x{rec["record_offset"]:X}]'
                       if repeated[rec['name']]>1 else rec['name'])
                self.dtz_tree.insert(parent,'end',iid=iid,text=label,
                      values=('—','Index only',f'{name}: GATG 0x{rec["record_offset"]:X}'))
                indexed_count+=1
        self.dtz_section_note.set(f'{texture_count} decoded textures · {indexed_count} additional indexed resources')
        if texture_count:
            first=next((i for i in self.dtz_tree.get_children('') if i.startswith('tex_')),None)
            if first is None:
                for group in self.dtz_tree.get_children(''):
                    first=next((i for i in self.dtz_tree.get_children(group) if i.startswith('tex_')),None)
                    if first:
                        break
            if first:
                self.dtz_tree.selection_set(first);self.dtz_tree.focus(first)
                n=int(first.split('_',1)[1])
                self.dtz_swizzle.set(doc.entries[n][1].swizzled)
                self.preview_dtz()
                return
        self.dtz_canvas.delete('all')
        self.dtz_image_ref=None
        self.dtz_details.set('Choose a decoded texture or indexed resource; unsupported native rasters remain inspection-only.')

    def _on_dtz_selected(self):
        selection=self.dtz_tree.selection()
        if not selection:
            return
        iid=selection[0]
        if iid.startswith('tex_'):
            if self.dtz_doc is not None:
                idx=int(iid.split('_',1)[1])
                if idx<len(self.dtz_doc.entries):
                    # Swizzle is a property of the selected raster, not a
                    # global DTZ flag inherited from the previous texture.
                    self.dtz_swizzle.set(self.dtz_doc.entries[idx][1].swizzled)
            self.preview_dtz()
            return
        self.dtz_canvas.delete('all')
        self.dtz_image_ref=None
        record=getattr(self,'dtz_index_rows',{}).get(iid)
        if record:
            self.dtz_details.set(
                f'{record["name"]}\nSection: {record["section"]}\n'
                f'Indexed resource at 0x{record["record_offset"]:X}\n'
                f'Resource reference: 0x{record["asset_pointer"]:X}\n'
                f'Node layout: {record.get("node_layout", "unknown")} · record type 0x{record.get("record_type", 0):X}\n'
                'The archive indexes this resource but its native image layout '
                'has not been decoded. Preview and replacement are disabled for this entry.')
        else:
            self.dtz_details.set('Select a decoded texture or a named resource in this section.')

    def _selected_texture(self,prefix):
        tr=getattr(self,prefix+'_tree');sel=tr.selection()
        if not sel:return None
        iid=sel[0]
        if not iid.startswith('tex_'):return None
        return int(iid[4:])

    def _show_image(self,prefix,image,label):
        canvas=getattr(self,prefix+'_canvas')
        img=image.convert('RGBA').copy()
        img.thumbnail((290,290),Image.Resampling.NEAREST)
        scale=max(1,min(290//max(1,img.width),290//max(1,img.height)))
        if scale>1:img=img.resize((img.width*scale,img.height*scale),Image.Resampling.NEAREST)
        # display alpha over solid visible background
        back=Image.new('RGBA',img.size,(28,33,48,255));back.alpha_composite(img)
        photo=ImageTk.PhotoImage(back.convert('RGB'),master=canvas)
        setattr(self,prefix+'_image_ref',photo)
        canvas.delete('all');canvas.create_image(155,155,image=photo,anchor='center')
        getattr(self,prefix+'_details').set(label)

    def open_txd(self):
        f=filedialog.askopenfilename(parent=self,title='Open PS2 RenderWare TXD',filetypes=[('TXD files','*.txd'),('All files','*.*')])
        if f:self.txd_source=Path(f);self.inspect_txd()

    def inspect_txd(self):
        if not self.txd_source:
            self.open_txd();return
        try:
            self.txd_doc=TXDDocument(self.txd_source.read_bytes());self.txd_doc.source_path=self.txd_source
            self._set_texture_sections('txd',['Native dictionary'])
            self._populate_txd_sections()
            self.status.set(f'TXD: {len(self.txd_doc.textures)} decoded indexed textures in {self.txd_source.name}')
        except Exception as exc:
            self.txd_doc=None;messagebox.showerror('TXD texture parsing failed',str(exc),parent=self)

    def preview_txd(self):
        i=self._selected_texture('txd')
        if self.txd_doc is None or i is None:return
        try:
            t=self.txd_doc.textures[i]
            self._show_image('txd',t.image(self.txd_doc.data,self.txd_swizzle.get()),f'{t.name}\n{t.width}×{t.height} · {t.bpp}bpp\n{t.note}')
        except Exception as e:self.txd_details.set(f'Cannot display selection: {e}')

    def export_txd_png(self):
        i=self._selected_texture('txd')
        if self.txd_doc is None or i is None:messagebox.showinfo('TXD','Select a texture first.',parent=self);return
        t=self.txd_doc.textures[i]
        f=filedialog.asksaveasfilename(parent=self,title='Export texture PNG',initialfile=f'{t.name}.png',defaultextension='.png',filetypes=[('PNG','*.png')])
        if f:t.image(self.txd_doc.data,self.txd_swizzle.get()).save(f);self.status.set(f'Exported {t.name} as PNG.')

    def _legacy_replace_txd_png(self):
        i=self._selected_texture('txd')
        if self.txd_doc is None or i is None:messagebox.showinfo('TXD','Select a texture first.',parent=self);return
        t=self.txd_doc.textures[i]
        f=filedialog.askopenfilename(parent=self,title=f'PNG replacement for {t.name} ({t.width}×{t.height})',filetypes=[('PNG','*.png')])
        if not f:return
        try:
            self.txd_doc.replace(i,f,self.txd_swizzle.get());self.preview_txd()
            self.status.set(f'Replaced {t.name} in memory; original file untouched. Save edited TXD as a new copy.')
        except Exception as e:messagebox.showerror('TXD replacement refused',str(e),parent=self)

    def _replace_texture_tm2(self, which):
        """Select an encoded TM2 and transfer its pixels+palette into native raster data.

        The TM2 container itself is not written into a TXD/GATG allocation.
        A modal confirmation makes pixel-layout and alpha-scale assumptions explicit.
        """
        index = self._selected_texture(which)
        doc = getattr(self, f'{which}_doc', None)
        if doc is None or index is None:
            tree = getattr(self, f'{which}_tree')
            selected = tree.selection()
            if which == 'dtz' and selected and selected[0].startswith('idx_'):
                messagebox.showwarning('Indexed resource: no decoded image',
                    'This resource is indexed under its DTZ section, but the underlying '
                    'pixel format and allocation have not been validated. TM2 replacement '
                    'is disabled for this entry so the archive cannot be corrupted.',parent=self)
            else:
                messagebox.showinfo('Replace from TM2', 'Select a decoded texture first.', parent=self)
            return
        t = doc.textures[index] if which == 'txd' else doc.entries[index][1]
        path = filedialog.askopenfilename(
            parent=self,
            title=f'Choose encoded TM2 replacement for {t.name} ({t.width}×{t.height} PAL{t.bpp})',
            filetypes=[('Encoded TIM2 textures', '*.tm2 *.tim2'), ('All files', '*.*')])
        if not path:
            return
        try:
            raw = Path(path).read_bytes()
            candidate = TIM2Document(raw)
            if (candidate.width, candidate.height, candidate.bpp) != (t.width, t.height, t.bpp):
                raise ValueError(
                    f'The selected TM2 is {candidate.width}×{candidate.height} PAL{candidate.bpp}. '
                    f'The target {t.name} is {t.width}×{t.height} PAL{t.bpp}. '
                    'Use the PNG → TIM2 tab to encode a correctly sized, matching-palette-depth TM2 first.')
        except Exception as exc:
            messagebox.showerror('Incompatible TM2', str(exc), parent=self)
            return

        dialog = tk.Toplevel(self)
        dialog.title(f'Replace {t.name} from TM2')
        dialog.transient(self)
        dialog.geometry('570x430')
        dialog.resizable(False, False)
        wrap = ttk.Frame(dialog, padding=14)
        wrap.pack(fill='both', expand=True)
        ttk.Label(wrap, text=f'Target: {t.name} · {t.width}×{t.height} · PAL{t.bpp}',
                  font=('Segoe UI', 11, 'bold')).pack(anchor='w')
        ttk.Label(wrap, text=f'Source: {Path(path).name} · {candidate.width}×{candidate.height} · PAL{candidate.bpp}',
                  wraplength=530).pack(anchor='w', pady=(5, 0))
        ttk.Label(wrap, text='Encoded TM2 pixels and palette will be transferred to this existing native texture. '
                  'Dimensions and pixel format must match. Other textures and container pointers will not be changed.',
                  wraplength=525).pack(anchor='w', pady=(8, 8))
        source_layout = tk.StringVar(value='Linear (Studio PNG → TIM2 output)')
        alpha_layout = tk.StringVar(value='Automatic from TM2 CLUT')
        ttk.Label(wrap, text='TM2 source pixel order').pack(anchor='w', pady=(3, 1))
        ttk.Combobox(wrap, textvariable=source_layout, state='readonly', width=48,
                     values=('Linear (Studio PNG → TIM2 output)', 'PS2 swizzled')).pack(anchor='w')
        ttk.Label(wrap, text='TM2 alpha interpretation').pack(anchor='w', pady=(10, 1))
        ttk.Combobox(wrap, textvariable=alpha_layout, state='readonly', width=48,
                     values=('Automatic from TM2 CLUT', 'PS2 0–128', 'PSP 0–255')).pack(anchor='w')
        ttk.Label(wrap, text='The preview deswizzle toggle is for visual comparison only: replacement always follows the verified native storage flag. PSP textures with mip levels retain their original lower mipmaps. '
                  'If a TM2 converted with an external tool appears scrambled after replacement, retry with the other TM2 source pixel order.',
                  wraplength=525, style='Muted.TLabel').pack(anchor='w', pady=(12, 0))
        buttons = ttk.Frame(wrap)
        buttons.pack(side='bottom', anchor='e', pady=12)
        ttk.Button(buttons, text='Cancel', command=dialog.destroy).pack(side='left', padx=5)
        def commit():
            try:
                alpha = {'Automatic from TM2 CLUT': None, 'PS2 0–128': False, 'PSP 0–255': True}[alpha_layout.get()]
                swz = source_layout.get() == 'PS2 swizzled'
                dst_swz = t.swizzled  # Stored layout is native; preview controls must not rewrite it
                if which == 'txd':
                    replace_txd_from_tm2(doc, index, raw,
                        source_swizzled=swz, target_swizzled=dst_swz, source_psp_alpha=alpha)
                    self.preview_txd()
                else:
                    replace_dtz_from_tm2(doc, index, raw,
                        source_swizzled=swz, target_swizzled=dst_swz, source_psp_alpha=alpha)
                    self.preview_dtz()
                self.status.set(f'Replaced {t.name} using encoded TM2 {Path(path).name}; source file unchanged. Save edited {which.upper()} as a NEW copy.')
                dialog.destroy()
            except Exception as exc:
                messagebox.showerror('TM2 replacement refused', str(exc), parent=dialog)
        ttk.Button(buttons, text='Replace texture', style='Accent.TButton', command=commit).pack(side='left', padx=5)
        dialog.grab_set()
        dialog.focus_set()

    def replace_txd_tm2(self):
        self._replace_texture_tm2('txd')

    def replace_dtz_tm2(self):
        self._replace_texture_tm2('dtz')

    def save_txd_copy(self):
        if self.txd_doc is None:return
        src=self.txd_source
        f=filedialog.asksaveasfilename(parent=self,title='Save modified TXD as',initialfile=src.stem+'_edited.TXD',defaultextension='.TXD',filetypes=[('TXD files','*.TXD')])
        if not f:return
        if Path(f).resolve()==self.txd_source.resolve():messagebox.showerror('Unsafe output','Select a different filename. The original TXD is preserved.',parent=self);return
        try:
            self.txd_doc.save(f)
            self.status.set(f'Saved TXD with {len(self.txd_doc.textures)} textures and verified reparse: {f}')
        except Exception as e:messagebox.showerror('TXD save failed',str(e),parent=self)

    def open_dtz(self):
        f=filedialog.askopenfilename(parent=self,title='Open GTA Stories GAME.DTZ',filetypes=[('DTZ files','*.dtz'),('All files','*.*')])
        if f:self.dtz_source=Path(f);self.inspect_dtz()

    def inspect_dtz(self):
        if not self.dtz_source:
            self.open_dtz();return
        try:
            self.dtz_doc=DTZDocument(self.dtz_source.read_bytes())
            if self.dtz_doc.entries:
                # VCS XET stores these verified 4-bit raster blocks linearly.
                # Other native indexed archives use PS2/PSP tile layouts.
                self.dtz_swizzle.set(self.dtz_doc.entries[0][1].swizzled)
            names=[('radar' if s['name'].startswith('XET@') else s['name']) for s in self.dtz_doc.sections]+list(self.dtz_doc.indexed_sections)
            self._set_texture_sections('dtz',names)
            self._populate_dtz_sections()
            self.status.set(f'DTZ: {len(self.dtz_doc.entries)} decoded textures and '
                            f'{sum(map(len,self.dtz_doc.indexed_sections.values()))} additional indexed resources '
                            f'across {len(set(names))} sections.')
        except Exception as e:self.dtz_doc=None;messagebox.showerror('DTZ parsing failed',str(e),parent=self)

    def _dtz_storage_order(self,texture):
        # PSP GATG raster descriptors encode the byte-tiled PSP layout.
        return True if texture.owner=='indexed_psp' else self.dtz_swizzle.get()

    def preview_dtz(self):
        i=self._selected_texture('dtz')
        if self.dtz_doc is None or i is None:return
        try:
            group,t=self.dtz_doc.entries[i]
            self._show_image('dtz',self.dtz_doc.preview(i,self._dtz_storage_order(t)),f'{t.name}\n{t.width}×{t.height} · {t.bpp}bpp\n{group}.chk · {t.note}')
        except Exception as e:self.dtz_details.set(f'Preview unavailable: {e}')

    def export_dtz_png(self):
        i=self._selected_texture('dtz')
        if self.dtz_doc is None or i is None:messagebox.showinfo('DTZ','Select a texture.',parent=self);return
        group,t=self.dtz_doc.entries[i]
        f=filedialog.asksaveasfilename(parent=self,title='Export texture PNG',initialfile=f'{t.name}.png',defaultextension='.png')
        if f:self.dtz_doc.preview(i,self._dtz_storage_order(t)).save(f);self.status.set(f'Exported {t.name}.png')

    def export_dtz_tim2(self):
        i=self._selected_texture('dtz')
        if self.dtz_doc is None or i is None:return
        group,t=self.dtz_doc.entries[i]
        f=filedialog.asksaveasfilename(parent=self,title='Export selected as TIM2',initialfile=t.name+'.tm2',defaultextension='.tm2')
        if not f:return
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            src=Path(folder)/'temp.png';self.dtz_doc.preview(i,self._dtz_storage_order(t)).save(src)
            Path(f).write_bytes(png_to_tim2(src,bpp=t.bpp,width=t.width,height=t.height,swizzled=False))
        self.status.set(f'Exported editable TIM2 for {t.name}. Confirm game/platform compatibility before patching.')

    def _legacy_replace_dtz_png(self):
        i=self._selected_texture('dtz')
        if self.dtz_doc is None or i is None:messagebox.showinfo('DTZ','Select a texture.',parent=self);return
        group,t=self.dtz_doc.entries[i]
        f=filedialog.askopenfilename(parent=self,title=f'Replace {t.name} ({t.width}×{t.height}) from PNG',filetypes=[('PNG','*.png')])
        if not f:return
        try:
            self.dtz_doc.replace(i,f,self.dtz_swizzle.get());self.preview_dtz()
            self.status.set(f'Replaced {t.name} in memory; CHK layout verified. Save a new DTZ.')
        except Exception as e:messagebox.showerror('DTZ replacement refused',str(e),parent=self)

    def save_dtz_copy(self):
        if self.dtz_doc is None:return
        f=filedialog.asksaveasfilename(parent=self,title='Save edited GAME.DTZ as',initialfile=self.dtz_source.stem+'_edited.DTZ',defaultextension='.DTZ',filetypes=[('DTZ','*.DTZ')])
        if not f:return
        if Path(f).resolve()==self.dtz_source.resolve():messagebox.showerror('Unsafe output','Use a new filename; original DTZ is never overwritten.',parent=self);return
        try:self.dtz_doc.save(f);self.status.set(f'Saved edited DTZ; structural round-trip verified (game rendering not guaranteed): {f}')
        except Exception as e:messagebox.showerror('DTZ save failed',str(e),parent=self)

    def save_dtz_raw(self):
        if self.dtz_doc is None:return
        f=filedialog.asksaveasfilename(parent=self,title='Export GATG decompressed data',initialfile='GAME.DAT',defaultextension='.DAT')
        if f:Path(f).write_bytes(self.dtz_doc.data);self.status.set('Exported decompressed GAME.DAT')

    def open_tim2_converter(self):
        win=tk.Toplevel(self);win.title('PNG → PlayStation TIM2 converter');win.geometry('690x450');win.configure(bg='#111827')
        body=ttk.Frame(win,padding=16);body.pack(fill='both',expand=True)
        ttk.Label(body,text='TIM2 PNG converter — compatible settings by game',font=('Segoe UI',13,'bold')).pack(anchor='w')
        vars={'src':tk.StringVar(), 'game':tk.StringVar(value='GTA Liberty City Stories PS2'),'bpp':tk.StringVar(value='4'), 'swz':tk.BooleanVar(value=True),'template':tk.StringVar()}
        row=ttk.Frame(body);row.pack(fill='x',pady=8)
        ttk.Entry(row,textvariable=vars['src']).pack(side='left',fill='x',expand=True)
        ttk.Button(row,text='Browse PNG…',command=lambda:vars['src'].set(filedialog.askopenfilename(parent=win,filetypes=[('PNG','*.png')]))).pack(side='left',padx=7)
        form=ttk.Frame(body);form.pack(fill='x')
        ttk.Label(form,text='Game / platform').grid(row=0,column=0,sticky='w',pady=6)
        ttk.Combobox(form,textvariable=vars['game'],values=GAMES,state='readonly',width=42).grid(row=0,column=1,sticky='w',padx=9)
        ttk.Label(form,text='Palette / pixel format').grid(row=1,column=0,sticky='w',pady=6)
        ttk.Combobox(form,textvariable=vars['bpp'],values=('4','8'),state='readonly',width=8).grid(row=1,column=1,sticky='w',padx=9)
        ttk.Checkbutton(form,text='PS2 swizzled pixel order',variable=vars['swz']).grid(row=2,column=0,columnspan=2,sticky='w',pady=6)
        ttk.Label(form,text='Optional original TIM2 template').grid(row=3,column=0,sticky='w',pady=6)
        trow=ttk.Frame(form);trow.grid(row=3,column=1,sticky='w',padx=9)
        ttk.Entry(trow,textvariable=vars['template'],width=34).pack(side='left')
        ttk.Button(trow,text='Browse…',command=lambda: vars['template'].set(filedialog.askopenfilename(parent=win,filetypes=[('TIM2 original','*.tm2'),('All files','*.*')]))).pack(side='left',padx=5)
        ttk.Label(body,text='An original .tm2 template preserves the target game’s actual picture metadata and palette format. Without one, conversion uses a generic indexed TIM2 layout, not a verified per-game encoder.',wraplength=630,style='Muted.TLabel').pack(anchor='w',pady=8)
        def convert():
            if not vars['src'].get():return
            path=Path(vars['src'].get())
            dst=filedialog.asksaveasfilename(parent=win,title='Save TIM2',initialfile=path.stem+'.tm2',defaultextension='.tm2',filetypes=[('TIM2','*.tm2')])
            if not dst:return
            try:
                binary=(png_to_tim2_template(path,vars['template'].get(),swizzled=vars['swz'].get()) if vars['template'].get().strip() else png_to_tim2(path,game=vars['game'].get(),bpp=int(vars['bpp'].get()),swizzled=vars['swz'].get()))
                Path(dst).write_bytes(binary)
                decoded=TIM2Document(binary)
                self.status.set(f'Exported {decoded.width}×{decoded.height} {decoded.bpp}bpp TIM2 ({len(binary):,} bytes).')
                messagebox.showinfo('TIM2 created',f'Created {dst}\n\n{decoded.width}×{decoded.height}; {decoded.bpp}bpp\nGame selection: {vars["game"].get()}\n\nTemplate: {vars['template'].get() or 'generic (no original template)'}\nExact compatibility still requires testing in the target game.',parent=win)
            except Exception as e:messagebox.showerror('TIM2 conversion failed',str(e),parent=win)
        ttk.Button(body,text='Convert PNG → TIM2…',command=convert,style='Accent.TButton').pack(anchor='e',pady=10)
